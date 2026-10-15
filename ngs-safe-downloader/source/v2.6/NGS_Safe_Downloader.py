#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NGS Safe Downloader
- Nextcloud public-share recursive downloader
- Resume via .part + HTTP Range
- Parallel file downloads
- Remote checksum verification when Nextcloud exposes oc:checksums
- Local SHA-256 manifest for every completed file
- Pause/resume, live speed/ETA and disk-space preflight
- Vendor .md5/.md5sum/.sha1/.sha1sum/.sha256/.sha256sum verification
- Automatic gzip/FASTQ.GZ CRC validation in full automatic mode

Designed for large pathology / NGS data transfers on unstable connections.
"""

from __future__ import annotations

import base64
import csv
import gzip
import html as html_lib
import hashlib
import json
import os
import queue
import random
import re
import shutil
import threading
import time
import traceback
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

try:
    import requests
    from requests.adapters import HTTPAdapter
except ImportError:
    raise SystemExit(
        "Bu program 'requests' paketini gerektirir.\n"
        "Komut: py -m pip install requests\n"
        "Veya yanındaki BASLAT_NGS_INDIRICI.bat dosyasını çalıştırın."
    )

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "NGS Safe Downloader"
APP_VERSION = "2.6"
CHUNK_SIZE = 8 * 1024 * 1024  # 8 MiB
CONNECT_TIMEOUT = 20
READ_TIMEOUT = 180
MAX_RETRIES = 50
STREAM_PROBE_BYTES = 4 * 1024 * 1024  # 4 MiB real payload probe
SERVER_PROTECT_THRESHOLD = 3
SERVER_COOLDOWN_SECONDS = 10 * 60
SERVER_HEALTH_PROBE_BYTES = 4 * 1024 * 1024
SERVER_HEALTH_READ_TIMEOUT = 75
INITIAL_RECONNECT_SECONDS = 30
AUTO_REPAIR_MAX_ROUNDS = 6
AUTO_REPAIR_PAUSE_SECONDS = 8
AUTO_RESUME_DELAY_SECONDS = 8
FULL_VERIFY_ALWAYS = True
PART_DIR_NAME = "_NGS_PARCA"
STATE_DIR_NAME = "NGS_Safe_Downloader"
APP_DIR = Path(__file__).resolve().parent
WORK_DIR_NAME = "_NGS_WORK"
VERIFICATION_ARCHIVE_DIR_NAME = "NGS_Dogrulamalar"
REPORT_LOCATION_MODE = "external"  # live work is external; final verification reports are archived beside the app
USER_AGENT = f"{APP_NAME}/{APP_VERSION}"

DAV = "DAV:"
OC = "http://owncloud.org/ns"

INVALID_WIN_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED_WIN_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


@dataclass
class RemoteFile:
    share_label: str
    url: str
    rel_path: str
    size: int
    etag: str = ""
    modified: str = ""
    checksums: Tuple[str, ...] = ()


@dataclass
class EndpointConfig:
    root_url: str
    auth: Optional[Tuple[str, str]]
    headers: Dict[str, str]
    label: str


def human_bytes(n: int) -> str:
    n = max(0, int(n or 0))
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    value = float(n)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{n} B"


def clean_component(name: str) -> str:
    name = urllib.parse.unquote(name)
    name = INVALID_WIN_CHARS.sub("_", name).strip().rstrip(". ")
    if not name:
        name = "_"
    if name.upper() in RESERVED_WIN_NAMES:
        name = f"_{name}"
    return name


def clean_rel_path(rel_path: str) -> str:
    parts = [clean_component(p) for p in rel_path.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return str(Path(*parts)) if parts else "downloaded_file"


def extract_share_token(url: str) -> Optional[Tuple[str, str]]:
    """Return (origin_with_optional_base_path, token) for /s/TOKEN or /index.php/s/TOKEN."""
    p = urllib.parse.urlparse(url.strip())
    m = re.search(r"(?P<prefix>.*?)(?:/index\.php)?/s/(?P<token>[^/?#]+)", p.path)
    if not m:
        return None
    prefix = m.group("prefix").rstrip("/")
    origin = f"{p.scheme or 'https'}://{p.netloc}{prefix}"
    return origin, m.group("token")


def build_session() -> requests.Session:
    s = requests.Session()
    adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=0)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "identity",  # byte ranges should match file bytes exactly
    })
    return s


def propfind_body() -> bytes:
    return b'''<?xml version="1.0" encoding="UTF-8"?>
<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
  <d:prop>
    <d:getlastmodified/>
    <d:getcontentlength/>
    <d:resourcetype/>
    <d:getetag/>
    <oc:size/>
    <oc:checksums/>
  </d:prop>
</d:propfind>'''


def endpoint_candidates(share_url: str, password: str) -> List[EndpointConfig]:
    parsed = extract_share_token(share_url)
    if not parsed:
        return []
    origin, token = parsed
    common = {"X-Requested-With": "XMLHttpRequest"}
    new_root = f"{origin}/public.php/dav/files/{urllib.parse.quote(token, safe='')}"
    legacy_root = f"{origin}/public.php/webdav"
    candidates: List[EndpointConfig] = []

    # New public WebDAV endpoint (Nextcloud 29+): password-protected shares use anonymous:password.
    if password:
        candidates.append(EndpointConfig(new_root, ("anonymous", password), common.copy(), "Nextcloud public DAV (new/password)"))
        candidates.append(EndpointConfig(new_root, (token, password), common.copy(), "Nextcloud public DAV (token/password compatibility)"))
    else:
        candidates.append(EndpointConfig(new_root, None, common.copy(), "Nextcloud public DAV (new/no-auth)"))
        candidates.append(EndpointConfig(new_root, (token, ""), common.copy(), "Nextcloud public DAV (new/token)"))

    # Legacy public.php/webdav: share token is normally the username.
    candidates.append(EndpointConfig(legacy_root, (token, password), common.copy(), "Nextcloud public WebDAV (legacy)"))
    return candidates


def request_propfind(session: requests.Session, cfg: EndpointConfig, url: str, depth: str = "1") -> requests.Response:
    headers = dict(cfg.headers)
    headers.update({"Depth": depth, "Content-Type": "application/xml; charset=utf-8"})
    return session.request(
        "PROPFIND", url,
        headers=headers,
        data=propfind_body(),
        auth=cfg.auth,
        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        allow_redirects=True,
    )


def choose_endpoint(share_url: str, password: str, log) -> EndpointConfig:
    candidates = endpoint_candidates(share_url, password)
    if not candidates:
        raise ValueError("Bu URL bir Nextcloud public paylaşım bağlantısına benzemiyor (/s/TOKEN bekleniyor).")
    session = build_session()
    errors = []
    try:
        for cfg in candidates:
            try:
                r = request_propfind(session, cfg, cfg.root_url, "1")
                if r.status_code == 207 and b"multistatus" in r.content.lower():
                    log(f"WebDAV bağlantısı tamam: {cfg.label}")
                    return cfg
                errors.append(f"{cfg.label}: HTTP {r.status_code}")
            except requests.RequestException as e:
                errors.append(f"{cfg.label}: {type(e).__name__}")
    finally:
        session.close()
    if password:
        hint = "Paylaşım parolasını ve bağlantıyı kontrol edin."
    else:
        hint = "Bağlantı parola korumalıysa parola alanını doldurun; sunucu WebDAV'ı kapatmış da olabilir."
    raise RuntimeError("WebDAV erişimi kurulamadı. " + hint + "\n" + " | ".join(errors))


def response_prop(response: ET.Element, tag: str) -> Optional[ET.Element]:
    for propstat in response.findall(f"{{{DAV}}}propstat"):
        status = (propstat.findtext(f"{{{DAV}}}status") or "")
        if " 200 " in status:
            prop = propstat.find(f"{{{DAV}}}prop")
            if prop is not None:
                el = prop.find(tag)
                if el is not None:
                    return el
    return None


def parse_listing(xml_bytes: bytes) -> List[dict]:
    root = ET.fromstring(xml_bytes)
    items = []
    for resp in root.findall(f"{{{DAV}}}response"):
        href = resp.findtext(f"{{{DAV}}}href") or ""
        rt = response_prop(resp, f"{{{DAV}}}resourcetype")
        is_dir = rt is not None and rt.find(f"{{{DAV}}}collection") is not None
        length_el = response_prop(resp, f"{{{DAV}}}getcontentlength")
        oc_size_el = response_prop(resp, f"{{{OC}}}size")
        etag_el = response_prop(resp, f"{{{DAV}}}getetag")
        modified_el = response_prop(resp, f"{{{DAV}}}getlastmodified")
        checksums_el = response_prop(resp, f"{{{OC}}}checksums")
        checksums = []
        if checksums_el is not None:
            for ch in checksums_el.findall(f"{{{OC}}}checksum"):
                if ch.text:
                    checksums.append(ch.text.strip())
        try:
            size = int((length_el.text if length_el is not None else None) or (oc_size_el.text if oc_size_el is not None else None) or 0)
        except (TypeError, ValueError):
            size = 0
        items.append({
            "href": href,
            "is_dir": is_dir,
            "size": size,
            "etag": (etag_el.text or "").strip() if etag_el is not None else "",
            "modified": (modified_el.text or "").strip() if modified_el is not None else "",
            "checksums": tuple(checksums),
        })
    return items


def canonical_url(origin_url: str, href: str) -> str:
    # href from WebDAV is generally already percent-encoded. Keep it intact.
    origin = urllib.parse.urlsplit(origin_url)
    base = f"{origin.scheme}://{origin.netloc}/"
    return urllib.parse.urljoin(base, href.lstrip("/"))


def path_from_href(href: str) -> List[str]:
    p = urllib.parse.urlsplit(href).path
    return [urllib.parse.unquote(x) for x in p.split("/") if x]


def relative_from_root(root_href: str, child_href: str) -> str:
    root_parts = path_from_href(root_href)
    child_parts = path_from_href(child_href)
    i = 0
    while i < min(len(root_parts), len(child_parts)) and root_parts[i] == child_parts[i]:
        i += 1
    return "/".join(child_parts[i:])


def scan_share(share_url: str, password: str, log, stop_event: threading.Event) -> Tuple[EndpointConfig, List[RemoteFile], int]:
    cfg = choose_endpoint(share_url, password, log)
    session = build_session()
    files: List[RemoteFile] = []
    total = 0
    share_token = extract_share_token(share_url)[1]
    share_label = share_token[:8]

    try:
        r = request_propfind(session, cfg, cfg.root_url, "1")
        r.raise_for_status()
        root_items = parse_listing(r.content)
        if not root_items:
            raise RuntimeError("Sunucu boş bir WebDAV yanıtı döndürdü.")
        root_href = root_items[0]["href"]

        # queue stores directory URLs + their hrefs, to preserve relative path.
        q = [(cfg.root_url, root_href)]
        seen_dirs = set()
        while q:
            if stop_event.is_set():
                raise InterruptedError("Tarama kullanıcı tarafından durduruldu.")
            dir_url, dir_href = q.pop(0)
            if dir_url in seen_dirs:
                continue
            seen_dirs.add(dir_url)
            r = request_propfind(session, cfg, dir_url, "1")
            if r.status_code != 207:
                raise RuntimeError(f"Klasör listelenemedi: HTTP {r.status_code} - {dir_url}")
            items = parse_listing(r.content)
            for item in items:
                href = item["href"]
                # First response is the queried directory itself.
                if path_from_href(href) == path_from_href(dir_href):
                    continue
                url = canonical_url(cfg.root_url, href)
                rel = relative_from_root(root_href, href)
                if not rel:
                    continue
                if item["is_dir"]:
                    q.append((url, href))
                else:
                    rf = RemoteFile(
                        share_label=share_label,
                        url=url,
                        rel_path=rel,
                        size=item["size"],
                        etag=item["etag"],
                        modified=item["modified"],
                        checksums=item["checksums"],
                    )
                    files.append(rf)
                    total += rf.size
            if len(files) and len(files) % 100 == 0:
                log(f"Tarama: {len(files)} dosya bulundu...")
    finally:
        session.close()
    files.sort(key=lambda x: x.rel_path.lower())
    return cfg, files, total


def parse_checksum_values(values: Iterable[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for raw in values:
        if not raw:
            continue
        # Usually one checksum per element; tolerate whitespace/comma separated variants.
        for piece in re.split(r"[\s,]+", raw.strip()):
            if ":" not in piece:
                continue
            alg, value = piece.split(":", 1)
            alg = alg.lower().replace("-", "")
            aliases = {"sha3": "sha3256", "sha3_256": "sha3256", "adler32": "adler32"}
            alg = aliases.get(alg, alg)
            if alg in {"md5", "sha1", "sha256", "sha3256", "adler32"} and value:
                out[alg] = value.lower()
    return out


def hash_file(path: Path, algorithms: Iterable[str]) -> Dict[str, str]:
    import zlib
    algs = set(algorithms)
    hashers = {}
    for alg in algs:
        if alg == "sha3256":
            hashers[alg] = hashlib.sha3_256()
        elif alg in {"md5", "sha1", "sha256"}:
            hashers[alg] = hashlib.new(alg)
    adler = 1
    with path.open("rb") as f:
        while True:
            b = f.read(CHUNK_SIZE)
            if not b:
                break
            for h in hashers.values():
                h.update(b)
            if "adler32" in algs:
                adler = zlib.adler32(b, adler)
    out = {k: h.hexdigest().lower() for k, h in hashers.items()}
    if "adler32" in algs:
        out["adler32"] = f"{adler & 0xffffffff:08x}"
    return out


def safe_mkdir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


def _job_key(dest: Path) -> str:
    """Readable, stable key for per-target working records."""
    try:
        absolute = str(Path(dest).expanduser().resolve())
    except Exception:
        absolute = str(Path(dest).expanduser())
    label = clean_component(Path(dest).name or "NGS")[:48]
    digest = hashlib.sha256(absolute.lower().encode("utf-8", "ignore")).hexdigest()[:8]
    return f"{label}_{digest}"


def tiny_state_root() -> Path:
    """Only a few KB of persistent state live on the system/app disk."""
    base = Path(os.environ.get("LOCALAPPDATA") or str(Path.home())) / STATE_DIR_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def work_dir(dest: Path) -> Path:
    """Live logs/reports stay on the target volume while a job is running."""
    dest = Path(dest)
    d = dest.parent / WORK_DIR_NAME / _job_key(dest)
    d.mkdir(parents=True, exist_ok=True)
    return d


def verification_archive_root() -> Path:
    """Small final verification records live beside the Python application."""
    d = APP_DIR / VERIFICATION_ARCHIVE_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def verification_archive_dir(dest: Path, ended: Optional[float] = None, create: bool = True) -> Path:
    ended = ended or time.time()
    stamp = time.strftime('%Y%m%d_%H%M%S', time.localtime(ended))
    d = verification_archive_root() / f"{_job_key(Path(dest))}_{stamp}"
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d

def latest_verification_archive(dest: Path) -> Path:
    root = verification_archive_root()
    prefix = _job_key(Path(dest)) + '_'
    matches = [x for x in root.iterdir() if x.is_dir() and x.name.startswith(prefix)]
    if not matches:
        return root
    return max(matches, key=lambda x: x.stat().st_mtime)

def _prune_empty_root(path: Path):
    try:
        if path.exists() and path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    except OSError:
        pass


def part_dir(dest: Path) -> Path:
    """Incomplete payloads live beside, never inside, the final data directory."""
    dest = Path(dest)
    d = dest.parent / PART_DIR_NAME / _job_key(dest)
    d.mkdir(parents=True, exist_ok=True)
    return d


def old_dedicated_part_path(dest: Path, rel_path: str) -> Path:
    """v1.8-v2.0 path: target/_NGS_PARCA/<relative>.part"""
    return Path(dest) / PART_DIR_NAME / (clean_rel_path(rel_path) + ".part")


def new_part_path(dest: Path, rel_path: str) -> Path:
    return part_dir(dest) / (clean_rel_path(rel_path) + ".part")


def legacy_part_path(dest: Path, rel_path: str) -> Path:
    target = Path(dest) / clean_rel_path(rel_path)
    return Path(str(target) + ".part")


def resolve_part_path(dest: Path, rel_path: str, migrate: bool = False) -> Path:
    """Return current part path; optionally migrate older part layouts."""
    newp = new_part_path(dest, rel_path)
    candidates = (old_dedicated_part_path(dest, rel_path), legacy_part_path(dest, rel_path))
    if newp.exists():
        return newp
    for oldp in candidates:
        if oldp.exists():
            if migrate:
                try:
                    safe_mkdir(newp.parent)
                    os.replace(oldp, newp)
                    return newp
                except OSError:
                    return oldp
            return oldp
    return newp


def migrate_legacy_parts(files: List[RemoteFile], dest: Path, log=None) -> Tuple[int, int]:
    moved = 0
    total = 0
    for rf in files:
        newp = new_part_path(dest, rf.rel_path)
        if newp.exists():
            continue
        for oldp in (old_dedicated_part_path(dest, rf.rel_path), legacy_part_path(dest, rf.rel_path)):
            try:
                if oldp.exists():
                    safe_mkdir(newp.parent)
                    n = oldp.stat().st_size
                    os.replace(oldp, newp)
                    moved += 1
                    total += n
                    break
            except OSError:
                pass
    oldroot = Path(dest) / PART_DIR_NAME
    if oldroot.exists():
        try:
            for d in sorted([x for x in oldroot.rglob('*') if x.is_dir()], key=lambda x: len(x.parts), reverse=True):
                try: d.rmdir()
                except OSError: pass
            try: oldroot.rmdir()
            except OSError: pass
        except OSError:
            pass
    if log and moved:
        log(f"Eski yarım indirmeler hedef dışındaki parça alanına taşındı: {moved} parça • {human_bytes(total)}")
    return moved, total


def clear_partial_files(files: List[RemoteFile], dest: Path, log=None) -> Tuple[int, int]:
    """Delete only partials belonging to the current remote listing."""
    count = 0
    total = 0
    for rf in files:
        seen = set()
        for pp in (new_part_path(dest, rf.rel_path), old_dedicated_part_path(dest, rf.rel_path), legacy_part_path(dest, rf.rel_path)):
            key = str(pp).lower()
            if key in seen:
                continue
            seen.add(key)
            try:
                if pp.exists():
                    n = pp.stat().st_size
                    pp.unlink()
                    count += 1
                    total += n
            except OSError as e:
                if log:
                    log(f"Parça silinemedi: {pp} | {e}")
    if log and count:
        log(f"Önceki yarım indirmeler temizlendi: {count} parça • {human_bytes(total)}")
    return count, total


def app_state_path() -> Path:
    base = os.environ.get('LOCALAPPDATA') or str(Path.home())
    d = Path(base) / STATE_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d / 'state.json'


def load_app_state() -> dict:
    try:
        return json.loads(app_state_path().read_text(encoding='utf-8'))
    except Exception:
        return {}


def save_app_state(data: dict):
    try:
        app_state_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        pass

def last_job_state_path() -> Path:
    # Tiny pointer/status file only; large logs never live here.
    return tiny_state_root() / 'LAST_JOB.json'

def load_last_job_state() -> dict:
    try:
        return json.loads(last_job_state_path().read_text(encoding='utf-8'))
    except Exception:
        return {}

def save_last_job_state(data: dict):
    try:
        payload = dict(data or {})
        payload['updated_epoch'] = time.time()
        payload['updated'] = time.strftime('%Y-%m-%d %H:%M:%S')
        tmp = last_job_state_path().with_suffix('.json.tmp')
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(tmp, last_job_state_path())
    except Exception:
        pass

def inspect_saved_live_status(dest_text: str) -> dict:
    try:
        dest = Path(dest_text).expanduser()
        f = dest.parent / WORK_DIR_NAME / _job_key(dest) / 'NGS_DOWNLOAD_STATUS.json'
        return json.loads(f.read_text(encoding='utf-8')) if f.exists() else {}
    except Exception:
        return {}

REPORT_DIR_NAME = '_NGS_RAPOR'

def set_report_location(in_app_dir: bool):
    # Kept for compatibility with older call sites. Live work stays external.
    global REPORT_LOCATION_MODE
    REPORT_LOCATION_MODE = 'external'

def report_dir(dest: Path) -> Path:
    p = work_dir(Path(dest))
    safe_mkdir(p)
    return p

def migrate_legacy_reports(dest: Path, log=None) -> int:
    """Move reports created by older versions out of the final data tree and app folder."""
    old = Path(dest) / REPORT_DIR_NAME
    new = report_dir(dest)
    moved = 0
    stamp = time.strftime('%Y%m%d_%H%M%S')
    candidates = []
    if old.exists() and old.is_dir():
        candidates.append(old)
    # v2.1/v2.2 could place records next to the Python file. Move only this job's folder if present.
    legacy_app = APP_DIR / 'NGS_Records' / _job_key(Path(dest))
    if legacy_app.exists() and legacy_app.is_dir():
        candidates.append(legacy_app)
    for source in candidates:
        try:
            for src in [x for x in source.rglob('*') if x.is_file()]:
                rel = src.relative_to(source)
                dst = new / rel
                safe_mkdir(dst.parent)
                if dst.exists():
                    dst = dst.with_name(f'legacy_{stamp}_{dst.name}')
                try:
                    shutil.move(str(src), str(dst)); moved += 1
                except OSError:
                    pass
            for d in sorted([x for x in source.rglob('*') if x.is_dir()], key=lambda x: len(x.parts), reverse=True):
                try: d.rmdir()
                except OSError: pass
            try: source.rmdir()
            except OSError: pass
        except OSError:
            pass
    # remove empty legacy NGS_Records root if possible
    try:
        (APP_DIR / 'NGS_Records').rmdir()
    except OSError:
        pass
    if log and moved:
        log(f'Eski rapor/log dosyaları hedef diskteki çalışma alanına taşındı: {moved} dosya')
    return moved


def _legacy_migrate_reports_unused(dest: Path, log=None) -> int:
    return 0

def prune_empty_part_tree(dest: Path):
    """Remove empty work directories after a completely successful transfer."""
    pd = Path(dest).parent / PART_DIR_NAME / _job_key(Path(dest))
    try:
        if pd.exists():
            for d in sorted([x for x in pd.rglob('*') if x.is_dir()], key=lambda x: len(x.parts), reverse=True):
                try: d.rmdir()
                except OSError: pass
            try: pd.rmdir()
            except OSError: pass
        root = Path(dest).parent / PART_DIR_NAME
        try: root.rmdir()
        except OSError: pass
    except OSError:
        pass


def set_mtime(path: Path, modified: str):
    if not modified:
        return
    try:
        dt = parsedate_to_datetime(modified)
        ts = dt.timestamp()
        os.utime(path, (ts, ts))
    except Exception:
        pass




def format_duration(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0 or seconds == float("inf"):
        return "--"
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}g {hours:02d}:{minutes:02d}"
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def calculate_missing_bytes(files: List[RemoteFile], dest: Path) -> int:
    missing = 0
    for rf in files:
        target = dest / clean_rel_path(rf.rel_path)
        part = resolve_part_path(dest, rf.rel_path, migrate=False)
        if target.exists() and (rf.size <= 0 or target.stat().st_size == rf.size):
            continue
        if rf.size > 0:
            have = 0
            if part.exists():
                try:
                    ps = part.stat().st_size
                    if 0 <= ps <= rf.size:
                        have = ps
                except OSError:
                    pass
            missing += max(0, rf.size - have)
    return missing


def guess_hash_algorithm(value: str) -> Optional[str]:
    v = value.strip().lower()
    if not re.fullmatch(r"[0-9a-f]+", v):
        return None
    return {32: "md5", 40: "sha1", 64: "sha256"}.get(len(v))


def parse_checksum_manifest_file(manifest: Path) -> List[Tuple[str, str, Path]]:
    """Return (algorithm, expected_hash, target_path) for common vendor checksum formats."""
    entries: List[Tuple[str, str, Path]] = []
    try:
        text = manifest.read_text(encoding="utf-8-sig", errors="replace")
    except Exception:
        return entries
    suffix_alg = {
        ".md5": "md5", ".md5sum": "md5",
        ".sha1": "sha1", ".sha1sum": "sha1",
        ".sha256": "sha256", ".sha256sum": "sha256",
    }.get(manifest.suffix.lower())
    nonempty = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    for line in nonempty:
        alg = suffix_alg
        expected = ""
        filename = ""

        # BSD style: MD5 (file.fastq.gz) = abcdef...
        m = re.match(r"(?i)^(MD5|SHA1|SHA256)\s*\((.+)\)\s*=\s*([0-9a-f]+)$", line)
        if m:
            alg = m.group(1).lower()
            expected = m.group(3).lower()
            filename = m.group(2).strip()
        else:
            # GNU style: hash [* ]filename
            m = re.match(r"^([0-9A-Fa-f]{32,64})\s+[* ]?(.+?)\s*$", line)
            if m:
                expected = m.group(1).lower()
                alg = alg or guess_hash_algorithm(expected)
                filename = m.group(2).strip()
            else:
                # filename : hash / filename = hash
                m = re.match(r"^(.+?)\s*[:=]\s*([0-9A-Fa-f]{32,64})\s*$", line)
                if m:
                    filename = m.group(1).strip()
                    expected = m.group(2).lower()
                    alg = alg or guess_hash_algorithm(expected)
                else:
                    # Sidecar .md5/.sha256 containing only the hash.
                    m = re.match(r"^([0-9A-Fa-f]{32,64})$", line)
                    if m and len(nonempty) == 1 and suffix_alg:
                        expected = m.group(1).lower()
                        alg = suffix_alg
                        filename = manifest.name[: -len(manifest.suffix)]

        if alg in {"md5", "sha1", "sha256"} and expected and filename:
            raw_name = filename.strip().strip('"').replace('\\', '/')
            raw_target = Path(raw_name)
            # Per-file sidecars from sequencing vendors often contain an absolute
            # Linux path from the vendor server.  The local sidecar filename is
            # authoritative for pairing when it clearly names the same data file.
            # Example:
            #   sample.fq.gz.md5sum -> /storeData/.../sample.fq.gz
            side_suffix = manifest.suffix.lower()
            paired_name = manifest.name[:-len(side_suffix)] if side_suffix in CHECKSUM_SIDECAR_SUFFIXES else ''
            if paired_name and Path(raw_name).name.lower() == paired_name.lower():
                target = manifest.parent / paired_name
            else:
                target = raw_target
                if not target.is_absolute():
                    target = manifest.parent / target
            entries.append((alg, expected, target))
    return entries


def find_vendor_checksum_manifests(dest: Path) -> List[Path]:
    names = {
        "md5sum.txt", "md5sums.txt", "checksums.md5", "checksum.md5", "md5.txt",
        "sha1sum.txt", "sha1sums.txt", "checksums.sha1", "sha1.txt",
        "sha256sum.txt", "sha256sums.txt", "checksums.sha256", "sha256.txt",
        "checksums.txt", "checksum.txt",
    }
    found: List[Path] = []
    for root, dirs, files in os.walk(dest):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fn in files:
            low = fn.lower()
            if low in names or low.endswith((".md5", ".md5sum", ".sha1", ".sha1sum", ".sha256", ".sha256sum")):
                found.append(Path(root) / fn)
    return sorted(set(found), key=lambda x: str(x).lower())


def verify_vendor_checksums(dest: Path, log, stop_event: threading.Event) -> Tuple[Path, int, int, int]:
    report = report_dir(dest) / "NGS_VENDOR_CHECKSUM_REPORT.csv"
    manifests = find_vendor_checksum_manifests(dest)
    rows = []
    cache: Dict[Tuple[str, str], str] = {}
    total_entries = 0
    ok_count = 0
    fail_count = 0
    if not manifests:
        rows.append({
            "status": "NO_MANIFEST", "algorithm": "", "expected": "", "actual": "",
            "target_file": "", "checksum_file": "", "note": "Vendor checksum dosyası bulunamadı"
        })
    for mf in manifests:
        if stop_event.is_set():
            break
        entries = parse_checksum_manifest_file(mf)
        if not entries:
            continue
        log(f"Vendor checksum okunuyor: {mf.relative_to(dest)} ({len(entries)} kayıt)")
        for alg, expected, target in entries:
            if stop_event.is_set():
                break
            total_entries += 1
            if not target.exists() or not target.is_file():
                fail_count += 1
                rows.append({
                    "status": "MISSING", "algorithm": alg, "expected": expected, "actual": "",
                    "target_file": str(target), "checksum_file": str(mf), "note": "Dosya bulunamadı"
                })
                continue
            key = (str(target.resolve()), alg)
            try:
                actual = cache.get(key)
                if actual is None:
                    actual = hash_file(target, [alg])[alg]
                    cache[key] = actual
                status = "OK" if actual.lower() == expected.lower() else "MISMATCH"
                if status == "OK":
                    ok_count += 1
                else:
                    fail_count += 1
                rows.append({
                    "status": status, "algorithm": alg, "expected": expected, "actual": actual,
                    "target_file": str(target), "checksum_file": str(mf),
                    "note": "" if status == "OK" else "Checksum uyuşmuyor"
                })
            except Exception as e:
                fail_count += 1
                rows.append({
                    "status": "ERROR", "algorithm": alg, "expected": expected, "actual": "",
                    "target_file": str(target), "checksum_file": str(mf), "note": str(e)
                })
    fields = ["status", "algorithm", "expected", "actual", "target_file", "checksum_file", "note"]
    with report.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    return report, total_entries, ok_count, fail_count


def verify_gzip_integrity(dest: Path, log, stop_event: threading.Event) -> Tuple[Path, int, int]:
    report = report_dir(dest) / "NGS_GZIP_INTEGRITY_REPORT.csv"
    rows = []
    ok_count = 0
    fail_count = 0
    gz_files = []
    for root, dirs, files in os.walk(dest):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fn in files:
            if fn.lower().endswith((".fastq.gz", ".fq.gz", ".vcf.gz", ".txt.gz", ".gz")) and not fn.endswith(".part"):
                gz_files.append(Path(root) / fn)
    for idx, path in enumerate(gz_files, 1):
        if stop_event.is_set():
            break
        log(f"GZIP CRC testi {idx}/{len(gz_files)}: {path.relative_to(dest)}")
        try:
            with gzip.open(path, "rb") as g:
                while True:
                    if stop_event.is_set():
                        break
                    chunk = g.read(CHUNK_SIZE)
                    if not chunk:
                        break
            if stop_event.is_set():
                break
            ok_count += 1
            rows.append({"status": "OK", "file": str(path), "note": "gzip CRC/stream OK"})
        except Exception as e:
            fail_count += 1
            rows.append({"status": "FAIL", "file": str(path), "note": str(e)})
    with report.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["status", "file", "note"])
        w.writeheader()
        w.writerows(rows)
    return report, ok_count, fail_count


# ------------------------------
# Link diagnosis + final audit
# ------------------------------

def diagnose_share_url(url: str, password: str = "") -> dict:
    """Best-effort, read-only diagnosis of a vendor delivery URL."""
    result = {
        "kind": "Unknown",
        "provider": "",
        "title": "",
        "version": "",
        "token": "",
        "dav": False,
        "detail": "",
    }
    parsed_token = extract_share_token(url)
    if parsed_token:
        origin, token = parsed_token
        result.update({"kind": "Nextcloud public share", "provider": urllib.parse.urlsplit(origin).netloc, "token": token, "dav": True})
    try:
        s = build_session()
        r = s.get(url, timeout=(CONNECT_TIMEOUT, 30), allow_redirects=True)
        text = r.text[:5_000_000] if r.ok else ""
        result["http_status"] = r.status_code
        result["final_url"] = r.url
        if text:
            mt = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']*)', text, re.I)
            if not mt:
                mt = re.search(r'<title>(.*?)</title>', text, re.I | re.S)
            if mt:
                result["title"] = html_lib.unescape(re.sub(r"\s+", " ", mt.group(1)).strip())
            mv = re.search(r'"versionstring"\s*:\s*"([^"]+)"', text)
            if not mv:
                mv = re.search(r'"version"\s*:\s*"([0-9.]+)"', text)
            if mv:
                result["version"] = mv.group(1)
            if "Nextcloud" in text or "public.php/dav" in text or "files_sharing" in text:
                result["kind"] = "Nextcloud public share"
                result["dav"] = bool(parsed_token)
            mp = re.search(r'"entity"\s*:\s*"([^"]+)"', text)
            if mp:
                result["provider"] = html_lib.unescape(mp.group(1))
        s.close()
    except Exception as e:
        result["detail"] = f"Web sayfası tanısı sınırlı: {e}"
    if result["kind"] == "Unknown":
        p = urllib.parse.urlsplit(url)
        if p.scheme in ("http", "https"):
            result["kind"] = "HTTP/HTTPS bağlantısı"
            result["provider"] = p.netloc
            result["detail"] = result["detail"] or "Bu sürümde otomatik indirme motoru Nextcloud public share için etkin."
    return result


def normalize_rel_for_match(rel: str) -> str:
    return Path(rel.replace('\\', '/')).as_posix().lstrip('./').lower()


CHECKSUM_HEX_LENGTHS = {'md5': 32, 'sha1': 40, 'sha256': 64}
CHECKSUM_SIDECAR_NAMES = {
    'md5sum.txt', 'md5sums.txt', 'sha1sum.txt', 'sha1sums.txt',
    'sha256sum.txt', 'sha256sums.txt', 'checksums.txt', 'checksum.txt',
}
CHECKSUM_SIDECAR_SUFFIXES = ('.md5', '.md5sum', '.sha1', '.sha1sum', '.sha256', '.sha256sum')


def parse_local_checksum_sidecars(dest: Path, remote_files: List[RemoteFile], log) -> Dict[str, Dict[str, str]]:
    """Read checksum sidecars already downloaded, without changing any data file."""
    known = {normalize_rel_for_match(rf.rel_path): rf for rf in remote_files}
    expected: Dict[str, Dict[str, str]] = {}

    def add(rel: str, alg: str, digest: str) -> bool:
        key = normalize_rel_for_match(rel)
        digest = digest.lower().strip()
        if key in known and alg in CHECKSUM_HEX_LENGTHS and len(digest) == CHECKSUM_HEX_LENGTHS[alg]:
            if re.fullmatch(r'[0-9a-f]+', digest):
                expected.setdefault(key, {})[alg] = digest
                return True
        return False

    for rf in remote_files:
        rel = rf.rel_path
        lowname = Path(rel).name.lower()
        suffix_alg = None
        if lowname.endswith(('.md5', '.md5sum')):
            suffix_alg = 'md5'
        elif lowname.endswith(('.sha1', '.sha1sum')):
            suffix_alg = 'sha1'
        elif lowname.endswith(('.sha256', '.sha256sum')):
            suffix_alg = 'sha256'
        is_named = lowname in CHECKSUM_SIDECAR_NAMES or any(x in lowname for x in ('checksum', 'md5sum', 'sha1sum', 'sha256sum'))
        if not suffix_alg and not is_named:
            continue
        path = dest / clean_rel_path(rel)
        if not path.exists() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
            continue
        try:
            text = path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        base_dir = Path(rel).parent.as_posix()
        found = 0
        for line in text.splitlines():
            line = line.strip().lstrip('\ufeff')
            if not line or line.startswith('#'):
                continue
            m = re.match(r'^([0-9A-Fa-f]{32}|[0-9A-Fa-f]{40}|[0-9A-Fa-f]{64})\s+[* ]?(.+?)\s*$', line)
            if m:
                digest, fname = m.groups()
                alg = {32:'md5', 40:'sha1', 64:'sha256'}[len(digest)]
                raw_name = fname.strip().strip('"').replace('\\','/')
                candidate = (Path(base_dir) / raw_name).as_posix() if base_dir not in ('', '.') and not Path(raw_name).is_absolute() else raw_name
                matched = add(candidate, alg, digest)
                if not matched:
                    # Vendor tools sometimes write an absolute Linux path into a sidecar.
                    # For a paired file such as sample.fq.gz.md5sum, the sidecar name
                    # itself is a stronger local pairing signal than that foreign path.
                    if suffix_alg == alg:
                        suffix = '.' + suffix_alg + ('sum' if lowname.endswith('.' + suffix_alg + 'sum') else '')
                        matched = add(rel[:-len(suffix)], alg, digest)
                    if not matched:
                        base = Path(raw_name).name
                        hits = [k for k in known if Path(k).name.lower() == base.lower()]
                        if len(hits) == 1:
                            matched = add(hits[0], alg, digest)
                if matched:
                    found += 1
                continue
            m = re.match(r'^(MD5|SHA1|SHA256)\s*\((.+)\)\s*=\s*([0-9A-Fa-f]+)\s*$', line, re.I)
            if m:
                name_alg, fname, digest = m.groups()
                alg = name_alg.lower()
                raw_name = fname.strip().replace('\\','/')
                candidate = (Path(base_dir) / raw_name).as_posix() if base_dir not in ('', '.') and not Path(raw_name).is_absolute() else raw_name
                matched = add(candidate, alg, digest)
                if not matched and suffix_alg == alg:
                    suffix = '.' + suffix_alg + ('sum' if lowname.endswith('.' + suffix_alg + 'sum') else '')
                    matched = add(rel[:-len(suffix)], alg, digest)
                if matched:
                    found += 1
        if suffix_alg and found == 0:
            m = re.search(rf'\b([0-9A-Fa-f]{{{CHECKSUM_HEX_LENGTHS[suffix_alg]}}})\b', text)
            if m:
                # Digest-only sidecars are common in sequencing deliveries, e.g. sample.fq.gz.md5sum.
                suffix = '.' + suffix_alg + ('sum' if lowname.endswith('.' + suffix_alg + 'sum') else '')
                target_rel = rel[:-len(suffix)]
                add(target_rel, suffix_alg, m.group(1))
    if expected:
        fastq_expected = sum(1 for key in expected if key.endswith(('.fastq.gz', '.fq.gz')))
        log(f"Kaynak checksum yan dosyaları okundu: {len(expected)} veri dosyası için MD5/SHA bulundu; FASTQ {fastq_expected}.")
    else:
        log("Yerel kaynak .md5/.sha1/.sha256 checksum kaydı bulunamadı veya eşleştirilemedi.")
    return expected


def hash_file_live(path: Path, algorithms: Iterable[str], progress_cb=None, stop_event: Optional[threading.Event] = None) -> Dict[str, str]:
    import zlib
    algs = set(algorithms)
    hashers = {}
    for alg in algs:
        if alg == 'sha3256':
            hashers[alg] = hashlib.sha3_256()
        elif alg in {'md5','sha1','sha256'}:
            hashers[alg] = hashlib.new(alg)
    adler = 1
    read_total = 0
    next_update = 0
    with path.open('rb') as f:
        while True:
            if stop_event is not None and stop_event.is_set():
                raise InterruptedError('Doğrulama durduruldu')
            b = f.read(CHUNK_SIZE)
            if not b:
                break
            read_total += len(b)
            for h in hashers.values():
                h.update(b)
            if 'adler32' in algs:
                adler = zlib.adler32(b, adler)
            if progress_cb and read_total >= next_update:
                progress_cb(read_total)
                next_update = read_total + 64 * 1024 * 1024
    if progress_cb:
        progress_cb(read_total)
    out = {k:h.hexdigest().lower() for k,h in hashers.items()}
    if 'adler32' in algs:
        out['adler32'] = f'{adler & 0xffffffff:08x}'
    return out


def gzip_integrity_test_one(path: Path) -> Tuple[bool, str]:
    try:
        with gzip.open(path, 'rb') as f:
            while f.read(CHUNK_SIZE):
                pass
        return True, 'gzip CRC/stream OK'
    except Exception as e:
        return False, f'gzip testi başarısız: {e}'


class VerificationManager:
    """Read-only live verification against the current remote share metadata."""
    def __init__(self, files: List[RemoteFile], dest: Path, log, progress_cb, status_cb,
                 item_cb, stop_event: threading.Event, gzip_test: bool = False):
        self.files = files
        self.dest = dest
        self.log = log
        self.progress_cb = progress_cb
        self.status_cb = status_cb
        self.item_cb = item_cb
        self.stop_event = stop_event
        self.gzip_test = gzip_test
        self.rows: List[dict] = []
        self.vendor_expected: Dict[str, Dict[str, str]] = {}
        self._remote_keys = {normalize_rel_for_match(rf.rel_path) for rf in files}
        self.total_bytes = sum(max(0, rf.size) for rf in files)
        self.processed_bytes = 0
        self.started = time.time()
        self.counts = {
            'verified_hash': 0, 'vendor_verified': 0, 'remote_verified': 0,
            'size_ok': 0, 'gzip_ok': 0, 'gzip_fail': 0,
            'fastq_total': 0, 'fastq_vendor_expected': 0,
            'fastq_vendor_ok': 0, 'fastq_crc_ok': 0, 'fastq_crc_fail': 0,
            'vendor_sidecar_expected': 0, 'vendor_sidecar_unparsed': 0,
            'missing': 0, 'partial': 0, 'mismatch': 0, 'error': 0,
        }

    def target_for(self, rf: RemoteFile) -> Path:
        return self.dest / clean_rel_path(rf.rel_path)

    def _server_health_probe(self) -> dict:
        """Small real payload Range probe used only after a protection cooldown."""
        candidates = [f for f in self.files if f.size >= SERVER_HEALTH_PROBE_BYTES]
        if not candidates:
            candidates = list(self.files)
        if not candidates:
            return {'ok': False, 'status': 0, 'error': 'uzak dosya bulunamadı', 'bytes': 0, 'speed_bps': 0}
        rf = candidates[0]
        session = build_session()
        started = time.perf_counter()
        got = 0
        status = 0
        try:
            headers = {
                'Accept': 'application/octet-stream',
                'Range': f'bytes=0-{SERVER_HEALTH_PROBE_BYTES-1}',
                'Cache-Control': 'no-cache',
            }
            with session.get(
                rf.url, headers=headers, auth=self.cfg.auth, stream=True,
                timeout=(CONNECT_TIMEOUT, SERVER_HEALTH_READ_TIMEOUT), allow_redirects=True
            ) as r:
                status = int(r.status_code)
                if status not in (200, 206):
                    return {'ok': False, 'status': status, 'error': f'HTTP {status}', 'bytes': 0, 'speed_bps': 0}
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if self.stop_event.is_set():
                        return {'ok': False, 'status': status, 'error': 'durduruldu', 'bytes': got, 'speed_bps': 0}
                    if not chunk:
                        continue
                    need = SERVER_HEALTH_PROBE_BYTES - got
                    if need <= 0:
                        break
                    got += min(len(chunk), need)
                    if got >= SERVER_HEALTH_PROBE_BYTES:
                        break
            elapsed = max(0.001, time.perf_counter() - started)
            # A partial but meaningful response is enough to prove the payload path is alive.
            min_ok = min(512 * 1024, max(1, rf.size if rf.size > 0 else 512 * 1024))
            ok = got >= min_ok
            return {'ok': ok, 'status': status, 'error': '' if ok else 'yetersiz veri akışı', 'bytes': got, 'speed_bps': got / elapsed}
        except requests.RequestException as e:
            return {'ok': False, 'status': request_status_code(e) or status, 'error': str(e), 'bytes': got, 'speed_bps': 0}
        except Exception as e:
            return {'ok': False, 'status': status, 'error': f'{type(e).__name__}: {e}', 'bytes': got, 'speed_bps': 0}
        finally:
            session.close()

    @staticmethod
    def is_fastq_gz(rel_path: str) -> bool:
        return rel_path.lower().endswith(('.fastq.gz', '.fq.gz'))

    def has_vendor_sidecar_for(self, rf: RemoteFile) -> bool:
        key = normalize_rel_for_match(rf.rel_path)
        remote_keys = self._remote_keys
        return any((key + suffix) in remote_keys for suffix in CHECKSUM_SIDECAR_SUFFIXES)

    def add_row(self, rf: RemoteFile, status: str, method: str, local_size: int,
                sha256: str = '', expected: str = '', note: str = ''):
        self.rows.append({
            'status': status, 'relative_path': rf.rel_path,
            'expected_size_bytes': rf.size, 'local_size_bytes': local_size,
            'verification_method': method, 'sha256_local': sha256,
            'expected_checksum': expected, 'remote_checksums': ' | '.join(rf.checksums),
            'etag': rf.etag, 'note': note,
        })
        self.item_cb(status, rf.rel_path, local_size, rf.size, method, note)

    def live_hash_update(self, rf: RemoteFile, index: int, total_files: int, read_bytes: int, method: str):
        expected = max(1, rf.size)
        pct = min(100.0, read_bytes / expected * 100.0) if rf.size > 0 else 0.0
        self.item_cb('CHECKING', rf.rel_path, read_bytes, rf.size, method, f'Okunuyor %{pct:.1f}')
        self.progress_cb(index - 1, total_files, self.processed_bytes + min(read_bytes, max(0, rf.size)), self.total_bytes, time.time() - self.started)

    def verify_one(self, rf: RemoteFile, index: int, total_files: int):
        if self.stop_event.is_set():
            return
        self.status_cb(f"Doğrulanıyor {index}/{total_files}: {rf.rel_path}")
        self.item_cb('CHECKING', rf.rel_path, 0, rf.size, '', 'Kontrol ediliyor...')
        target = self.target_for(rf)
        part = resolve_part_path(self.dest, rf.rel_path, migrate=False)
        is_fastq = self.is_fastq_gz(rf.rel_path)
        if not target.exists():
            if part.exists():
                n = part.stat().st_size
                self.counts['partial'] += 1
                self.add_row(rf, 'PARTIAL', 'size', n, note=f'.part mevcut: {n}/{rf.size} bayt')
            else:
                self.counts['missing'] += 1
                self.add_row(rf, 'MISSING', 'none', 0, note='Dosya henüz indirilmemiş')
            self.progress_cb(index, total_files, self.processed_bytes, self.total_bytes, time.time() - self.started)
            return
        try:
            local_size = target.stat().st_size
            if rf.size > 0 and local_size != rf.size:
                self.counts['mismatch'] += 1
                self.add_row(rf, 'SIZE_MISMATCH', 'size', local_size,
                             note=f'Beklenen {rf.size}, yerel {local_size} bayt')
                self.processed_bytes += min(local_size, max(0, rf.size))
                self.progress_cb(index, total_files, self.processed_bytes, self.total_bytes, time.time() - self.started)
                return

            remote = parse_checksum_values(rf.checksums)
            vendor = self.vendor_expected.get(normalize_rel_for_match(rf.rel_path), {})
            sidecar_expected = self.has_vendor_sidecar_for(rf)
            if is_fastq and sidecar_expected and not vendor:
                self.counts['vendor_sidecar_unparsed'] += 1

            expected_hashes = dict(vendor)
            expected_hashes.update(remote)
            local_sha = ''
            hash_ok = False
            hash_sources = []
            hash_bad = []

            if expected_hashes:
                algs = set(expected_hashes) | {'sha256'}
                local = hash_file_live(
                    target, algs,
                    lambda n: self.live_hash_update(rf, index, total_files, n, 'checksum+sha256'),
                    self.stop_event,
                )
                local_sha = local.get('sha256', '')
                for alg, exp in expected_hashes.items():
                    if local.get(alg, '').lower() != exp.lower():
                        hash_bad.append(f'{alg}: beklenen {exp}, bulunan {local.get(alg, "")}')
                if not hash_bad:
                    hash_ok = True
                    self.counts['verified_hash'] += 1
                    if remote:
                        self.counts['remote_verified'] += 1
                        hash_sources.append('REMOTE')
                    if vendor:
                        self.counts['vendor_verified'] += 1
                        hash_sources.append('VENDOR')
                        if is_fastq:
                            self.counts['fastq_vendor_ok'] += 1
            else:
                local_sha = hash_file_live(
                    target, ['sha256'],
                    lambda n: self.live_hash_update(rf, index, total_files, n, 'sha256'),
                    self.stop_event,
                )['sha256']

            # Deep gzip validation is an independent second layer. It is run even
            # when vendor/remote checksums are present, so MD5/SHA and GZIP CRC
            # can both be reported for the same FASTQ file.
            gzip_checked = False
            gzip_ok = False
            gzip_note = ''
            if self.gzip_test and is_fastq:
                gzip_checked = True
                self.item_cb('CHECKING', rf.rel_path, local_size, rf.size, 'gzip_crc', 'GZIP/CRC açma testi...')
                gzip_ok, gzip_note = gzip_integrity_test_one(target)
                if gzip_ok:
                    self.counts['gzip_ok'] += 1
                    self.counts['fastq_crc_ok'] += 1
                else:
                    self.counts['gzip_fail'] += 1
                    self.counts['fastq_crc_fail'] += 1

            expected_text = ' | '.join(f'{k}:{v}' for k, v in expected_hashes.items())
            methods = []
            notes = []
            status = ''

            if expected_hashes:
                methods.append('+'.join(hash_sources) if hash_sources else '+'.join(sorted(expected_hashes)))
                if hash_bad:
                    notes.extend(hash_bad)
                else:
                    notes.append('Kaynak checksum eşleşti')
            else:
                methods.append('size+local_sha256')
                notes.append('Boyut eşleşti; kaynak checksum yayımlanmamış')
                if is_fastq and sidecar_expected:
                    notes.append('UYARI: checksum sidecar mevcut ancak içerik eşleştirilemedi')

            if gzip_checked:
                methods.append('gzip_crc')
                notes.append(gzip_note)

            if hash_bad or (gzip_checked and not gzip_ok):
                self.counts['mismatch'] += 1
                if hash_bad and gzip_checked and not gzip_ok:
                    status = 'CHECKSUM_AND_GZIP_ERROR'
                elif hash_bad:
                    status = 'CHECKSUM_MISMATCH'
                else:
                    status = 'GZIP_ERROR'
            elif is_fastq and sidecar_expected and not vendor:
                # A vendor checksum file exists but could not be associated with
                # the FASTQ. Do not silently call this fully verified.
                self.counts['error'] += 1
                status = 'VENDOR_CHECKSUM_UNREADABLE'
            elif expected_hashes and gzip_checked:
                status = 'VERIFIED_HASH_AND_GZIP'
            elif expected_hashes:
                status = 'VERIFIED_HASH'
            elif gzip_checked:
                status = 'GZIP_OK_SIZE_OK'
                self.counts['size_ok'] += 1
            else:
                status = 'SIZE_OK_NO_SOURCE_HASH'
                self.counts['size_ok'] += 1

            self.add_row(rf, status, '+'.join(methods), local_size, local_sha, expected_text, '; '.join(notes))
            self.processed_bytes += max(0, rf.size if rf.size > 0 else local_size)
        except InterruptedError:
            return
        except Exception as e:
            self.counts['error'] += 1
            try:
                n = target.stat().st_size
            except Exception:
                n = 0
            self.add_row(rf, 'VERIFY_ERROR', 'error', n, note=str(e))
        self.progress_cb(index, total_files, self.processed_bytes, self.total_bytes, time.time() - self.started)

    def write_report(self) -> Tuple[Path, Path]:
        outdir = report_dir(self.dest)
        csv_report = outdir / 'DOGRULAMA_DETAY.csv'
        txt_report = outdir / 'DOGRULAMA_SONUCU.txt'
        fields = ['status','relative_path','expected_size_bytes','local_size_bytes','verification_method',
                  'sha256_local','expected_checksum','remote_checksums','etag','note']
        ordered = sorted(self.rows, key=lambda x: x['relative_path'].lower())
        with csv_report.open('w', newline='', encoding='utf-8-sig') as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for row in ordered:
                w.writerow(row)
        problems = self.counts['missing'] + self.counts['partial'] + self.counts['mismatch'] + self.counts['error']
        deep_state = ('OTOMATİK' if self.gzip_test else 'ATLANDI')
        vendor_complete = (self.counts['fastq_total'] > 0 and self.counts['fastq_vendor_ok'] == self.counts['fastq_total'])
        crc_complete = (not self.gzip_test) or (self.counts['fastq_total'] > 0 and self.counts['fastq_crc_ok'] == self.counts['fastq_total'])
        trust = 'TAM DOĞRULANDI' if problems == 0 and vendor_complete and crc_complete else ('DOĞRULANDI - CRC ÇALIŞTIRILMADI' if problems == 0 and vendor_complete and not self.gzip_test else 'KONTROL GEREKİYOR')
        lines = [
            'NGS SAFE DOWNLOADER - DOGRULAMA SONUCU',
            '=' * 48,
            f'Tarih: {time.strftime("%Y-%m-%d %H:%M:%S")}',
            f'Hedef: {self.dest}',
            f'Toplam dosya: {len(self.files)}',
            f'FASTQ.GZ: {self.counts["fastq_total"]}',
            f'Checksum yan dosyası bulundu: {self.counts["vendor_sidecar_expected"]}/{self.counts["fastq_total"]}',
            f'Checksum yan dosyası okundu/eşleştirildi: {self.counts["fastq_vendor_expected"]}/{self.counts["fastq_total"]}',
            f'Kaynak MD5/SHA eşleşmesi: {self.counts["fastq_vendor_ok"]}/{self.counts["fastq_total"]} FASTQ',
            f'FASTQ.GZ GZIP/CRC: {deep_state} | başarılı {self.counts["fastq_crc_ok"]}/{self.counts["fastq_total"]}',
            f'Sorun: {problems}',
            f'SONUÇ: {trust}',
            '',
            'DOSYA DETAYLARI',
            '-' * 48,
        ]
        for row in ordered:
            exp = row.get('expected_size_bytes', 0) or 0
            loc = row.get('local_size_bytes', 0) or 0
            note = row.get('note', '') or ''
            method = row.get('verification_method', '') or ''
            lines.append(f"[{row.get('status','')}] {row.get('relative_path','')} | {human_bytes(loc)}/{human_bytes(exp)} | {method} | {note}")
        txt_report.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        return txt_report, csv_report

    def run(self):
        self.status_cb('Final doğrulama hazırlanıyor...')
        self.vendor_expected = parse_local_checksum_sidecars(self.dest, self.files, self.log)
        total_files = len(self.files)
        fastq_total = sum(1 for rf in self.files if self.is_fastq_gz(rf.rel_path))
        sidecar_targets = sum(1 for rf in self.files if self.is_fastq_gz(rf.rel_path) and self.has_vendor_sidecar_for(rf))
        parsed_fastq = sum(1 for rf in self.files if self.is_fastq_gz(rf.rel_path) and normalize_rel_for_match(rf.rel_path) in self.vendor_expected)
        self.counts['fastq_total'] = fastq_total
        self.counts['vendor_sidecar_expected'] = sidecar_targets
        self.counts['fastq_vendor_expected'] = parsed_fastq
        self.log(f'FASTQ.GZ: {fastq_total} dosya; checksum yan dosyası bulunan: {sidecar_targets}; doğru FASTQ ile eşleştirilen: {parsed_fastq}.')
        if fastq_total and sidecar_targets == fastq_total and parsed_fastq == fastq_total:
            self.log(f'Kaynak MD5/SHA sidecar eşleştirmesi OK: {parsed_fastq}/{fastq_total}.')
        elif sidecar_targets and parsed_fastq < sidecar_targets:
            self.log(f'UYARI: {sidecar_targets - parsed_fastq} checksum yan dosyası FASTQ ile eşleştirilemedi.')
        if self.gzip_test:
            self.log('FASTQ.GZ bütünlük kontrolü otomatik: kaynak checksum kontrolünden bağımsız GZIP/CRC katmanı uygulanacak.')
        else:
            self.log('Derin FASTQ.GZ GZIP/CRC testi kapalı.')
        self.log(f'Final doğrulama başladı: {total_files} dosya. Salt-okunur; dosyalara dokunulmayacak.')
        for i, rf in enumerate(self.files, start=1):
            if self.stop_event.is_set():
                break
            self.verify_one(rf, i, total_files)
        report, csv_report = self.write_report()
        self.status_cb('Doğrulama durduruldu' if self.stop_event.is_set() else 'Doğrulama tamamlandı')
        bad_statuses = {
            'PARTIAL', 'MISSING', 'SIZE_MISMATCH', 'CHECKSUM_MISMATCH',
            'CHECKSUM_AND_GZIP_ERROR', 'GZIP_ERROR', 'VENDOR_CHECKSUM_UNREADABLE',
            'VERIFY_ERROR'
        }
        problem_rows = [dict(row) for row in self.rows if row.get('status') in bad_statuses]
        problems = self.counts['missing'] + self.counts['partial'] + self.counts['mismatch'] + self.counts['error']
        vendor_complete = (self.counts['fastq_total'] > 0 and self.counts['fastq_vendor_ok'] == self.counts['fastq_total'])
        crc_complete = (not self.gzip_test) or (self.counts['fastq_total'] > 0 and self.counts['fastq_crc_ok'] == self.counts['fastq_total'])
        full_verified = bool(problems == 0 and vendor_complete and crc_complete)
        return {
            'report': str(report), 'csv_report': str(csv_report),
            'problem_rows': problem_rows,
            'problem_rel_paths': [row.get('relative_path','') for row in problem_rows if row.get('relative_path')],
            'full_verified': full_verified,
            'verification_trust': ('TAM DOĞRULANDI' if full_verified else ('KONTROL GEREKİYOR' if problems or not vendor_complete else 'DOĞRULANDI - CRC ÇALIŞTIRILMADI')),
            **self.counts
        }

class AdaptiveConcurrencyController:
    """Shared adaptive limiter for long-running payload GETs.

    Metadata probes are useful but can overestimate what a storage backend can
    sustain for multi-GB FASTQ streams. This controller reduces payload
    concurrency when several transient network/server failures occur close
    together, then cautiously recovers after a long stable period.
    """
    def __init__(self, initial_limit: int, max_limit: int, log, status_cb=None):
        self.initial_limit = max(1, int(initial_limit))
        self.max_limit = max(self.initial_limit, int(max_limit))
        self.current_limit = self.initial_limit
        self.min_limit_seen = self.initial_limit
        self.log = log
        self.status_cb = status_cb
        self.cond = threading.Condition()
        self.active = 0
        self.problem_times: List[float] = []
        self.started_at = time.time()
        self.last_problem = self.started_at
        self.last_change = 0.0
        self.large_successes = 0
        self.changes: List[dict] = []

    def acquire(self, stop_event: threading.Event, pause_event: threading.Event) -> bool:
        with self.cond:
            while not stop_event.is_set():
                if pause_event.is_set():
                    self.cond.wait(timeout=0.25)
                    continue
                if self.active < self.current_limit:
                    self.active += 1
                    return True
                self.cond.wait(timeout=0.5)
        return False

    def release(self):
        with self.cond:
            self.active = max(0, self.active - 1)
            self.cond.notify_all()

    def _announce(self, old: int, new: int, reason: str):
        direction = 'azaltıldı' if new < old else 'yükseltildi'
        msg = f'Adaptif paralellik {direction}: {old} → {new} • {reason}'
        self.log(msg)
        if self.status_cb:
            try:
                self.status_cb(msg)
            except Exception:
                pass

    def report_problem(self, reason: str):
        now = time.time()
        with self.cond:
            self.problem_times = [t for t in self.problem_times if now - t <= 240]
            self.problem_times.append(now)
            self.last_problem = now
            self.large_successes = 0
            # Two transient failures in a short window are enough to downshift,
            # but do not cascade 4→2→1 from the same simultaneous timeout wave.
            if len(self.problem_times) >= 2 and now - self.last_change >= 25:
                old = self.current_limit
                if old >= 4:
                    new = 2
                elif old == 3:
                    new = 2
                elif old == 2:
                    new = 1
                else:
                    new = 1
                if new < old:
                    self.current_limit = new
                    self.min_limit_seen = min(self.min_limit_seen, new)
                    self.last_change = now
                    self.problem_times.clear()
                    self.changes.append({'time': now, 'old': old, 'new': new, 'reason': reason})
                    self.cond.notify_all()
                    self._announce(old, new, reason)

    def report_large_success(self, byte_count: int):
        if byte_count < 64 * 1024 * 1024:
            return
        now = time.time()
        with self.cond:
            self.large_successes += 1
            # Recovery is intentionally slow: at least 10 minutes without a
            # transient failure plus several successfully completed large files.
            if self.current_limit < self.max_limit and self.large_successes >= 3 and now - self.last_problem >= 600 and now - self.last_change >= 600:
                old = self.current_limit
                new = 2 if old == 1 else min(4, self.max_limit)
                if new > old:
                    self.current_limit = new
                    self.last_change = now
                    self.large_successes = 0
                    self.changes.append({'time': now, 'old': old, 'new': new, 'reason': 'uzun süre stabil büyük dosya aktarımı'})
                    self.cond.notify_all()
                    self._announce(old, new, 'uzun süre stabil büyük dosya aktarımı')

    def retry_delay(self, attempt: int) -> float:
        base = min(90.0, float(2 ** min(max(0, attempt - 1), 6)))
        if self.current_limit < self.initial_limit:
            base = max(base, 8.0)
        # Jitter prevents all timed-out payload streams from reconnecting in lock-step.
        return min(120.0, base + random.uniform(0.25 * base, 0.75 * base) + random.uniform(0.0, 2.5))


def is_transient_request_problem(exc: requests.RequestException) -> bool:
    if isinstance(exc, (requests.exceptions.ReadTimeout, requests.exceptions.ConnectTimeout, requests.exceptions.ConnectionError)):
        return True
    resp = getattr(exc, 'response', None)
    code = getattr(resp, 'status_code', None)
    return bool(code == 429 or (isinstance(code, int) and code >= 500))


def request_status_code(exc: BaseException) -> Optional[int]:
    resp = getattr(exc, 'response', None)
    code = getattr(resp, 'status_code', None)
    return int(code) if isinstance(code, int) else None


class ServerProtectionGate:
    """Pause payload GETs when a single-connection server stays unhealthy.

    After several consecutive 503/502/504/timeouts at payload concurrency 1, the
    downloader stops hammering the server. It waits, performs one small Range
    health probe, and only releases queued downloads when the probe succeeds.
    """
    def __init__(self, health_probe, log, status_cb=None, sound_cb=None,
                 stop_event: Optional[threading.Event] = None,
                 pause_event: Optional[threading.Event] = None,
                 threshold: int = SERVER_PROTECT_THRESHOLD,
                 cooldown_seconds: float = SERVER_COOLDOWN_SECONDS):
        self.health_probe = health_probe
        self.log = log
        self.status_cb = status_cb
        self.sound_cb = sound_cb
        self.stop_event = stop_event or threading.Event()
        self.pause_event = pause_event or threading.Event()
        self.threshold = max(1, int(threshold))
        self.cooldown_seconds = max(1.0, float(cooldown_seconds))
        self.cond = threading.Condition()
        self.consecutive = 0
        self.cooldown_until = 0.0
        self.health_checking = False
        self.activations = 0
        self.health_checks = 0
        self.recoveries = 0
        self.total_wait_seconds = 0.0
        self._cooldown_started_at = 0.0
        self._last_status_second = None
        self._alerted_this_outage = False

    @property
    def active(self) -> bool:
        with self.cond:
            return self.cooldown_until > 0 or self.health_checking

    def _status(self, msg: str):
        if self.status_cb:
            try:
                self.status_cb(msg)
            except Exception:
                pass

    def _sound(self, kind: str):
        if self.sound_cb:
            try:
                self.sound_cb(kind)
            except Exception:
                pass

    def report_success(self):
        with self.cond:
            if self.cooldown_until <= 0 and not self.health_checking:
                self.consecutive = 0

    def report_single_connection_problem(self, reason: str):
        """Count only failures that happened while payload concurrency was 1."""
        with self.cond:
            if self.cooldown_until > 0 or self.health_checking:
                return
            self.consecutive += 1
            if self.consecutive < self.threshold:
                return
            now = time.time()
            self.activations += 1
            self.cooldown_until = now + self.cooldown_seconds
            self._cooldown_started_at = now
            self._last_status_second = None
            self.consecutive = 0
            self._alerted_this_outage = True
            self.cond.notify_all()
        mins = self.cooldown_seconds / 60.0
        self.log(f'SUNUCU KORUMA MODU: tek bağlantıda art arda {self.threshold} geçici hata ({reason}). '
                 f'{mins:.0f} dakika istek gönderilmeyecek; sonra {human_bytes(SERVER_HEALTH_PROBE_BYTES)} sağlık testi yapılacak.')
        self._status(f'SUNUCU BEKLEME MODU • yaklaşık {mins:.0f} dk sonra otomatik sağlık testi')
        self._sound('server')

    def _finish_wait_accounting(self, now: float):
        if self._cooldown_started_at > 0:
            self.total_wait_seconds += max(0.0, now - self._cooldown_started_at)
            self._cooldown_started_at = 0.0

    def before_request(self) -> bool:
        """Block during protection mode; one waiting worker owns each health probe."""
        while not self.stop_event.is_set():
            while self.pause_event.is_set() and not self.stop_event.is_set():
                time.sleep(0.25)
            if self.stop_event.is_set():
                return False

            do_probe = False
            with self.cond:
                now = time.time()
                if self.cooldown_until <= 0 and not self.health_checking:
                    return True
                if self.cooldown_until > now:
                    remaining = max(0, int(self.cooldown_until - now))
                    # Update the visible countdown at most once every 10 seconds.
                    bucket = remaining // 10
                    if bucket != self._last_status_second:
                        self._last_status_second = bucket
                        mm, ss = divmod(remaining, 60)
                        self._status(f'SUNUCU BEKLEME MODU • sağlık testine {mm:02d}:{ss:02d}')
                    self.cond.wait(timeout=min(1.0, max(0.1, self.cooldown_until - now)))
                    continue
                if self.health_checking:
                    self.cond.wait(timeout=0.5)
                    continue
                self.health_checking = True
                do_probe = True

            if do_probe:
                self._status('Sunucu sağlık testi yapılıyor...')
                self.log(f'Sunucu sağlık testi başladı: {human_bytes(SERVER_HEALTH_PROBE_BYTES)} gerçek Range isteği.')
                try:
                    result = self.health_probe() or {}
                except Exception as e:
                    result = {'ok': False, 'status': 0, 'error': f'{type(e).__name__}: {e}', 'speed_bps': 0, 'bytes': 0}
                now = time.time()
                with self.cond:
                    self.health_checks += 1
                    self.health_checking = False
                    if result.get('ok'):
                        self._finish_wait_accounting(now)
                        self.cooldown_until = 0.0
                        self.consecutive = 0
                        self.recoveries += 1
                        self._last_status_second = None
                        self._alerted_this_outage = False
                        self.cond.notify_all()
                        recovered = True
                    else:
                        self._finish_wait_accounting(now)
                        self.cooldown_until = now + self.cooldown_seconds
                        self._cooldown_started_at = now
                        self._last_status_second = None
                        self.cond.notify_all()
                        recovered = False
                if recovered:
                    speed = human_bytes(result.get('speed_bps', 0)) + '/sn' if result.get('speed_bps', 0) else '--'
                    self.log(f'SUNUCU DÜZELDİ: sağlık testi başarılı • HTTP {result.get("status","-")} • '
                             f'{human_bytes(result.get("bytes",0))} • {speed}. İndirme otomatik devam ediyor.')
                    self._status('Sunucu yeniden erişilebilir • indirme otomatik devam ediyor')
                    self._sound('recovered')
                    return True
                else:
                    self.log(f'Sunucu hâlâ kullanılamıyor: sağlık testi başarısız • HTTP {result.get("status","-")} • '
                             f'{result.get("error","")}. {self.cooldown_seconds/60:.0f} dk daha beklenecek.')
                    self._status(f'Sunucu hâlâ sorunlu • {self.cooldown_seconds/60:.0f} dk daha bekleniyor')
                    # Do not beep every ten minutes; the initial outage alert is enough.
                    continue
        return False

    def stats(self) -> dict:
        with self.cond:
            wait = self.total_wait_seconds
            if self._cooldown_started_at > 0:
                wait += max(0.0, time.time() - self._cooldown_started_at)
            return {
                'server_protection_activations': self.activations,
                'server_health_checks': self.health_checks,
                'server_recoveries': self.recoveries,
                'server_wait_seconds': wait,
            }


class DownloadManager:
    def __init__(self, cfg: EndpointConfig, files: List[RemoteFile], dest: Path, workers: int, log, progress_cb, status_cb, stop_event: threading.Event, pause_event: threading.Event, sound_cb=None):
        self.cfg = cfg
        self.files = files
        self.dest = dest
        self.workers = max(1, min(4, workers))  # initial adaptive payload concurrency
        self.pool_workers = max(self.workers, min(4, max(1, len(files))))
        self.log = log
        self.progress_cb = progress_cb
        self.status_cb = status_cb
        self.stop_event = stop_event
        self.pause_event = pause_event
        self.lock = threading.Lock()
        self.status_lock = threading.Lock()
        self.total_size = sum(f.size for f in files)
        self.done_bytes = 0
        self.file_bytes = {}
        self.ok = 0
        self.failed = 0
        self.skipped = 0
        self.manifest_rows = []
        self.started = time.time()
        self._metric_ts = self.started
        self._metric_bytes = 0
        self.speed_bps = 0.0
        self._status_last_write = 0.0
        self.controller = AdaptiveConcurrencyController(self.workers, self.pool_workers, self.log, self.status_cb)
        self.sound_cb = sound_cb
        self.server_gate = ServerProtectionGate(
            self._server_health_probe, self.log, self.status_cb, self.sound_cb,
            self.stop_event, self.pause_event
        )

    def target_for(self, rf: RemoteFile) -> Path:
        return self.dest / clean_rel_path(rf.rel_path)

    def _server_health_probe(self) -> dict:
        """Small real payload Range probe used only after a protection cooldown."""
        candidates = [f for f in self.files if f.size >= SERVER_HEALTH_PROBE_BYTES]
        if not candidates:
            candidates = list(self.files)
        if not candidates:
            return {'ok': False, 'status': 0, 'error': 'uzak dosya bulunamadı', 'bytes': 0, 'speed_bps': 0}
        rf = candidates[0]
        session = build_session()
        started = time.perf_counter()
        got = 0
        status = 0
        try:
            headers = {
                'Accept': 'application/octet-stream',
                'Range': f'bytes=0-{SERVER_HEALTH_PROBE_BYTES-1}',
                'Cache-Control': 'no-cache',
            }
            with session.get(
                rf.url, headers=headers, auth=self.cfg.auth, stream=True,
                timeout=(CONNECT_TIMEOUT, SERVER_HEALTH_READ_TIMEOUT), allow_redirects=True
            ) as r:
                status = int(r.status_code)
                if status not in (200, 206):
                    return {'ok': False, 'status': status, 'error': f'HTTP {status}', 'bytes': 0, 'speed_bps': 0}
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if self.stop_event.is_set():
                        return {'ok': False, 'status': status, 'error': 'durduruldu', 'bytes': got, 'speed_bps': 0}
                    if not chunk:
                        continue
                    need = SERVER_HEALTH_PROBE_BYTES - got
                    if need <= 0:
                        break
                    got += min(len(chunk), need)
                    if got >= SERVER_HEALTH_PROBE_BYTES:
                        break
            elapsed = max(0.001, time.perf_counter() - started)
            # A partial but meaningful response is enough to prove the payload path is alive.
            min_ok = min(512 * 1024, max(1, rf.size if rf.size > 0 else 512 * 1024))
            ok = got >= min_ok
            return {'ok': ok, 'status': status, 'error': '' if ok else 'yetersiz veri akışı', 'bytes': got, 'speed_bps': got / elapsed}
        except requests.RequestException as e:
            return {'ok': False, 'status': request_status_code(e) or status, 'error': str(e), 'bytes': got, 'speed_bps': 0}
        except Exception as e:
            return {'ok': False, 'status': status, 'error': f'{type(e).__name__}: {e}', 'bytes': got, 'speed_bps': 0}
        finally:
            session.close()

    @staticmethod
    def is_fastq_gz(rel_path: str) -> bool:
        return rel_path.lower().endswith(('.fastq.gz', '.fq.gz'))

    def has_vendor_sidecar_for(self, rf: RemoteFile) -> bool:
        key = normalize_rel_for_match(rf.rel_path)
        remote_keys = self._remote_keys
        return any((key + suffix) in remote_keys for suffix in CHECKSUM_SIDECAR_SUFFIXES)

    def initial_completed_bytes(self):
        total = 0
        for rf in self.files:
            target = self.target_for(rf)
            part = resolve_part_path(self.dest, rf.rel_path, migrate=True)
            if target.exists() and (rf.size <= 0 or target.stat().st_size == rf.size):
                total += target.stat().st_size
            elif part.exists() and (rf.size <= 0 or part.stat().st_size <= rf.size):
                total += part.stat().st_size
        self.done_bytes = total
        self._metric_bytes = self.done_bytes
        self.progress_cb(self.done_bytes, self.total_size, 0, len(self.files), 0.0, None)

    def add_progress(self, delta: int):
        if delta <= 0:
            return
        now = time.time()
        with self.lock:
            self.done_bytes += delta
            done = self.done_bytes
            dt = now - self._metric_ts
            if dt >= 0.5:
                db = max(0, done - self._metric_bytes)
                inst = db / dt if dt > 0 else 0.0
                self.speed_bps = inst if self.speed_bps <= 0 else (0.30 * inst + 0.70 * self.speed_bps)
                self._metric_ts = now
                self._metric_bytes = done
            speed = self.speed_bps
            eta = ((self.total_size - done) / speed) if speed > 1024 and self.total_size > done else None
        self.progress_cb(done, self.total_size, self.ok + self.skipped, len(self.files), speed, eta)
        self.write_live_status(done, speed, eta)

    def wait_if_paused(self):
        while self.pause_event.is_set() and not self.stop_event.is_set():
            time.sleep(0.2)

    def write_live_status(self, done: int, speed: float, eta: Optional[float]):
        now = time.time()
        with self.status_lock:
            if now - self._status_last_write < 5:
                return
            self._status_last_write = now
        try:
            payload = {
                "app": APP_NAME,
                "version": APP_VERSION,
                "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                "status": "paused" if self.pause_event.is_set() else "downloading",
                "total_bytes": self.total_size,
                "done_bytes": done,
                "percent": (done / self.total_size * 100) if self.total_size else 0,
                "speed_bytes_sec": speed,
                "eta_seconds": eta,
                "files_total": len(self.files),
                "files_completed": self.ok + self.skipped,
                "files_failed": self.failed,
                "parallel_initial": self.controller.initial_limit,
                "parallel_current": self.controller.current_limit,
                "parallel_min": self.controller.min_limit_seen,
            }
            tmp = report_dir(self.dest) / "NGS_DOWNLOAD_STATUS.json.tmp"
            final = report_dir(self.dest) / "NGS_DOWNLOAD_STATUS.json"
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, final)
        except Exception:
            pass

    def get_session(self) -> requests.Session:
        s = build_session()
        s.headers.update(self.cfg.headers)
        return s

    def verify_existing(self, rf: RemoteFile, target: Path) -> Tuple[bool, str, str]:
        if not target.exists():
            return False, "", ""
        if rf.size > 0 and target.stat().st_size != rf.size:
            return False, "", ""
        remote = parse_checksum_values(rf.checksums)
        if remote:
            local = hash_file(target, remote.keys())
            for alg, expected in remote.items():
                if local.get(alg, "") != expected.lower():
                    return False, "", f"remote {alg} checksum uyuşmuyor"
            sha256 = local.get("sha256") or hash_file(target, ["sha256"])["sha256"]
            return True, sha256, "remote checksum OK"
        sha256 = hash_file(target, ["sha256"])["sha256"]
        return True, sha256, "boyut OK; remote checksum yok"

    def download_one(self, rf: RemoteFile) -> dict:
        if self.stop_event.is_set():
            return {"status": "stopped", "file": rf}
        target = self.target_for(rf)
        safe_mkdir(target.parent)
        part = resolve_part_path(self.dest, rf.rel_path, migrate=True)
        safe_mkdir(part.parent)

        # Completed file: verify rather than redownload.
        if target.exists() and (rf.size <= 0 or target.stat().st_size == rf.size):
            ok, sha256, note = self.verify_existing(rf, target)
            if ok:
                with self.lock:
                    self.skipped += 1
                    self.manifest_rows.append(self.manifest_row(rf, target, sha256, "EXISTING_OK", note))
                return {"status": "skipped", "file": rf, "path": str(target), "note": note}
            bad = target.with_suffix(target.suffix + ".bad")
            try:
                if bad.exists():
                    bad.unlink()
                target.replace(bad)
            except OSError:
                pass

        # A .part larger than server file cannot be resumed safely.
        if part.exists() and rf.size > 0 and part.stat().st_size > rf.size:
            part.unlink()

        session = self.get_session()
        try:
            for attempt in range(1, MAX_RETRIES + 1):
                self.wait_if_paused()
                if self.stop_event.is_set():
                    return {"status": "stopped", "file": rf}
                existing = part.stat().st_size if part.exists() else 0
                headers = {"Accept": "application/octet-stream"}
                if existing > 0:
                    headers["Range"] = f"bytes={existing}-"
                    if rf.etag:
                        headers["If-Range"] = rf.etag
                acquired = False
                try:
                    if not self.server_gate.before_request():
                        return {"status": "stopped", "file": rf}
                    acquired = self.controller.acquire(self.stop_event, self.pause_event)
                    if not acquired:
                        return {"status": "stopped", "file": rf}
                    with session.get(
                        rf.url,
                        headers=headers,
                        auth=self.cfg.auth,
                        stream=True,
                        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                        allow_redirects=True,
                    ) as r:
                        if r.status_code in (401, 403, 404):
                            raise RuntimeError(f"HTTP {r.status_code} (kalıcı erişim hatası)")
                        if r.status_code == 416 and rf.size > 0 and existing == rf.size:
                            # Already complete in .part.
                            pass
                        else:
                            r.raise_for_status()
                            # Server ignored Range and sent full file: restart part.
                            if existing > 0 and r.status_code == 200:
                                self.add_progress(-existing)  # no-op by design; correct below under lock
                                with self.lock:
                                    self.done_bytes = max(0, self.done_bytes - existing)
                                existing = 0
                                mode = "wb"
                            elif existing > 0 and r.status_code == 206:
                                mode = "ab"
                            else:
                                mode = "wb"
                                if existing > 0:
                                    with self.lock:
                                        self.done_bytes = max(0, self.done_bytes - existing)
                                    existing = 0
                            with part.open(mode) as f:
                                for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                                    self.wait_if_paused()
                                    if self.stop_event.is_set():
                                        return {"status": "stopped", "file": rf}
                                    if not chunk:
                                        continue
                                    f.write(chunk)
                                    self.add_progress(len(chunk))
                    if acquired:
                        self.controller.release()
                        acquired = False

                    actual = part.stat().st_size if part.exists() else 0
                    if rf.size > 0 and actual != rf.size:
                        raise IOError(f"Eksik dosya: {actual}/{rf.size} bayt")

                    # Verify remote checksums if present; always compute local SHA-256 for manifest.
                    remote = parse_checksum_values(rf.checksums)
                    algs = set(remote.keys()) | {"sha256"}
                    local = hash_file(part, algs)
                    for alg, expected in remote.items():
                        if local.get(alg, "") != expected.lower():
                            # Full-size but corrupt content must not be retried from EOF.
                            bad_size = part.stat().st_size if part.exists() else 0
                            try:
                                part.unlink(missing_ok=True)
                                self.log(f"Checksum hatalı tam parça silindi; dosya yeniden indirilecek: {rf.rel_path}")
                            except OSError:
                                bad = Path(str(part) + f".checksum_bad_{int(time.time())}")
                                try:
                                    os.replace(part, bad)
                                    self.log(f"Silinemeyen checksum-hatalı parça karantinaya alındı: {bad.name}")
                                except OSError:
                                    pass
                            with self.lock:
                                self.done_bytes = max(0, self.done_bytes - bad_size)
                                self._metric_bytes = min(self._metric_bytes, self.done_bytes)
                            raise IOError(f"Checksum uyuşmazlığı ({alg}); dosya sıfırdan yeniden indirilecek")

                    os.replace(part, target)
                    set_mtime(target, rf.modified)
                    note = "remote checksum OK" if remote else "boyut OK; yerel SHA-256 üretildi"
                    with self.lock:
                        self.ok += 1
                        self.manifest_rows.append(self.manifest_row(rf, target, local["sha256"], "OK", note))
                    self.server_gate.report_success()
                    self.controller.report_large_success(rf.size or actual)
                    return {"status": "ok", "file": rf, "path": str(target), "note": note}

                except requests.RequestException as e:
                    if acquired:
                        self.controller.release()
                        acquired = False
                    if is_transient_request_problem(e):
                        limit_at_error = self.controller.current_limit
                        code = request_status_code(e)
                        reason = f'HTTP {code}' if code else type(e).__name__
                        self.controller.report_problem(reason)
                        # Protection mode is intentionally armed only when a request actually
                        # failed while payload concurrency was already 1. This avoids turning a
                        # normal 4→2→1 adaptive downshift into an immediate ten-minute pause.
                        if limit_at_error == 1 and (code in (502, 503, 504) or isinstance(e, (requests.exceptions.ReadTimeout, requests.exceptions.ConnectTimeout, requests.exceptions.ConnectionError))):
                            self.server_gate.report_single_connection_problem(reason)
                    if attempt >= MAX_RETRIES:
                        raise
                    delay = self.controller.retry_delay(attempt)
                    self.log(f"Tekrar denenecek ({attempt}/{MAX_RETRIES}) {rf.rel_path}: {e} | {delay:.1f} sn • aktif paralellik {self.controller.current_limit}")
                    end_wait = time.time() + delay
                    while time.time() < end_wait:
                        self.wait_if_paused()
                        if self.stop_event.is_set():
                            return {"status": "stopped", "file": rf}
                        time.sleep(0.2)
                except (OSError, IOError) as e:
                    if acquired:
                        self.controller.release()
                        acquired = False
                    if attempt >= MAX_RETRIES:
                        raise
                    delay = min(30.0, 1.0 + attempt * 1.5 + random.uniform(0.0, 2.0))
                    self.log(f"Tekrar denenecek ({attempt}/{MAX_RETRIES}) {rf.rel_path}: {e} | {delay:.1f} sn")
                    end_wait = time.time() + delay
                    while time.time() < end_wait:
                        self.wait_if_paused()
                        if self.stop_event.is_set():
                            return {"status": "stopped", "file": rf}
                        time.sleep(0.2)
                except RuntimeError:
                    if acquired:
                        self.controller.release()
                        acquired = False
                    raise
                finally:
                    if acquired:
                        self.controller.release()
                        acquired = False
        finally:
            session.close()

    def manifest_row(self, rf: RemoteFile, target: Path, sha256: str, status: str, note: str) -> dict:
        part = resolve_part_path(self.dest, rf.rel_path, migrate=False)
        try:
            local_size = target.stat().st_size if target.exists() else (part.stat().st_size if part.exists() else 0)
        except OSError:
            local_size = 0
        return {
            "status": status,
            "relative_path": rf.rel_path,
            "local_path": str(target),
            "expected_size_bytes": rf.size,
            "local_size_bytes": local_size,
            "sha256_local": sha256,
            "remote_checksums": " | ".join(rf.checksums),
            "etag": rf.etag,
            "remote_modified": rf.modified,
            "note": note,
        }

    def write_manifest(self):
        safe_mkdir(self.dest)
        manifest = report_dir(self.dest) / "NGS_DOWNLOAD_MANIFEST.csv"
        fields = ["status", "relative_path", "local_path", "expected_size_bytes", "local_size_bytes", "sha256_local", "remote_checksums", "etag", "remote_modified", "note"]
        with manifest.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for row in sorted(self.manifest_rows, key=lambda x: x["relative_path"].lower()):
                w.writerow(row)
        return manifest

    def run(self):
        self.initial_completed_bytes()
        self.status_cb("İndirme başladı")
        failed_paths: List[str] = []
        futures = {}
        with ThreadPoolExecutor(max_workers=self.pool_workers, thread_name_prefix="ngsdl") as pool:
            for rf in self.files:
                fut = pool.submit(self.download_one, rf)
                futures[fut] = rf
            for fut in as_completed(futures):
                rf = futures[fut]
                try:
                    result = fut.result()
                    st = result.get("status")
                    if st == "ok":
                        self.log(f"OK  {rf.rel_path}")
                    elif st == "skipped":
                        self.log(f"VAR {rf.rel_path} ({result.get('note','')})")
                    elif st == "stopped":
                        self.log(f"YARIM {rf.rel_path}")
                except Exception as e:
                    with self.lock:
                        self.failed += 1
                        failed_paths.append(rf.rel_path)
                        target = self.target_for(rf)
                        self.manifest_rows.append(self.manifest_row(rf, target, "", "FAILED", str(e)))
                    self.log(f"HATA {rf.rel_path}: {e}")

        # Manifest should account for every remote file, including partial/not-started files.
        with self.lock:
            represented = {row.get("relative_path") for row in self.manifest_rows}
            for rf in self.files:
                if rf.rel_path in represented:
                    continue
                target = self.target_for(rf)
                part = resolve_part_path(self.dest, rf.rel_path, migrate=False)
                if part.exists():
                    note = f"partial {part.stat().st_size}/{rf.size} bytes" if rf.size else f"partial {part.stat().st_size} bytes"
                    self.manifest_rows.append(self.manifest_row(rf, target, "", "PARTIAL", note))
                else:
                    self.manifest_rows.append(self.manifest_row(rf, target, "", "NOT_STARTED", "İndirme tamamlanmadı"))
        manifest = self.write_manifest()
        elapsed = max(0.1, time.time() - self.started)
        self.status_cb("Durduruldu" if self.stop_event.is_set() else "Tamamlandı")
        try:
            status_path = report_dir(self.dest) / "NGS_DOWNLOAD_STATUS.json"
            if status_path.exists():
                data = json.loads(status_path.read_text(encoding="utf-8"))
            else:
                data = {}
            data.update({
                "status": "stopped" if self.stop_event.is_set() else "download_complete_pending_verification",
                "phase": "download" if self.stop_event.is_set() else "download_complete",
                "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                "files_completed": self.ok + self.skipped,
                "files_failed": self.failed,
            })
            status_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        result = {
            "ok": self.ok,
            "skipped": self.skipped,
            "failed": self.failed,
            "failed_paths": list(dict.fromkeys(failed_paths)),
            "manifest": str(manifest),
            "elapsed": elapsed,
            "parallel_initial": self.controller.initial_limit,
            "parallel_final": self.controller.current_limit,
            "parallel_min": self.controller.min_limit_seen,
            "parallel_changes": list(self.controller.changes),
        }
        result.update(self.server_gate.stats())
        return result



# ------------------------------
# Safe parallel-connection test
# ------------------------------

def _parallel_metadata_probe(cfg: EndpointConfig) -> dict:
    """One small Depth:0 PROPFIND probe. Does not download an NGS data file."""
    session = build_session()
    started = time.perf_counter()
    try:
        r = request_propfind(session, cfg, cfg.root_url, "0")
        latency = max(0.0001, time.perf_counter() - started)
        return {
            "ok": r.status_code == 207,
            "status": int(r.status_code),
            "latency": latency,
            "error": "" if r.status_code == 207 else f"HTTP {r.status_code}",
        }
    except Exception as e:
        return {
            "ok": False,
            "status": 0,
            "latency": max(0.0001, time.perf_counter() - started),
            "error": f"{type(e).__name__}: {e}",
        }
    finally:
        session.close()


def choose_parallel_recommendation(results: List[dict]) -> Tuple[int, int]:
    """Return (stable_max, recommended) from completed benchmark levels.

    Recommendation is intentionally conservative: choose the smallest stable
    level reaching >=90% of the best metadata request throughput, cap at 8.
    This estimates server/concurrency health, not raw WAN bandwidth saturation.
    """
    stable = [r for r in results if r.get("success") == r.get("level") and r.get("level", 0) > 0]
    if not stable:
        return 1, 1
    stable_max = max(int(r["level"]) for r in stable)
    best_ops = max(float(r.get("ops_per_sec", 0.0)) for r in stable) or 0.0
    eligible = []
    for r in stable:
        level = int(r["level"])
        if level > 8:
            continue
        if best_ops <= 0 or float(r.get("ops_per_sec", 0.0)) >= best_ops * 0.90:
            eligible.append(level)
    if eligible:
        recommended = min(eligible)
    else:
        recommended = min(stable_max, 6)
    # One connection can work, but two gives resilience without being aggressive.
    if stable_max >= 2:
        recommended = max(2, recommended)
    recommended = max(1, min(recommended, stable_max, 8))
    return stable_max, recommended


def test_parallel_connections(cfg: EndpointConfig, log, stop_event: threading.Event,
                              progress_cb=None, max_workers: int = 12) -> dict:
    """Safely test concurrent WebDAV metadata requests at increasing levels.

    No FASTQ/BAM payload is intentionally downloaded. Stop on the first level
    that produces a server error/rate-limit/timeout, to avoid hammering vendor
    infrastructure.
    """
    levels = [n for n in (1, 2, 4, 6, 8, 10, 12) if n <= max(1, int(max_workers))]
    if max_workers not in levels and 1 <= int(max_workers) <= 12:
        levels.append(int(max_workers))
        levels = sorted(set(levels))
    results = []
    for level in levels:
        if stop_event.is_set():
            break
        started = time.perf_counter()
        probes = []
        with ThreadPoolExecutor(max_workers=level, thread_name_prefix="ngsprobe") as pool:
            futures = [pool.submit(_parallel_metadata_probe, cfg) for _ in range(level)]
            for fut in as_completed(futures):
                if stop_event.is_set():
                    break
                try:
                    probes.append(fut.result())
                except Exception as e:
                    probes.append({"ok": False, "status": 0, "latency": 0.0, "error": str(e)})
        elapsed = max(0.0001, time.perf_counter() - started)
        success = sum(1 for x in probes if x.get("ok"))
        lat = sorted(float(x.get("latency", 0.0)) for x in probes if x.get("latency") is not None)
        median = lat[len(lat)//2] if lat else 0.0
        statuses = sorted({str(x.get("status") or x.get("error") or "ERR") for x in probes})
        row = {
            "level": level,
            "success": success,
            "elapsed": elapsed,
            "median_latency": median,
            "ops_per_sec": success / elapsed,
            "statuses": ", ".join(statuses),
        }
        results.append(row)
        log(f"Paralellik testi {level}: {success}/{level} başarılı • medyan {median:.2f} sn • {row['ops_per_sec']:.1f} istek/sn • {row['statuses']}")
        if progress_cb:
            progress_cb(row, results)
        if success < level:
            log("İlk kararsız/rate-limit seviyesinde test güvenlik için durduruldu.")
            break
        time.sleep(0.25)
    stable_max, recommended = choose_parallel_recommendation(results)
    return {
        "results": results,
        "stable_max": stable_max,
        "recommended": recommended,
        "ceiling_reached": bool(results and results[-1].get("level") == max(levels) and results[-1].get("success") == results[-1].get("level")),
    }


def test_real_stream(cfg: EndpointConfig, files: List[RemoteFile], log, stop_event: threading.Event) -> dict:
    """Read only a tiny byte range from one representative large data file.

    This complements metadata PROPFIND probes. It intentionally reads at most
    STREAM_PROBE_BYTES and closes the response, so it never downloads a full
    FASTQ during the benchmark.
    """
    candidates = [f for f in files if f.size >= 16 * 1024 * 1024 and f.rel_path.lower().endswith(('.fq.gz', '.fastq.gz', '.bam', '.cram'))]
    if not candidates:
        candidates = [f for f in files if f.size >= 16 * 1024 * 1024]
    if not candidates or stop_event.is_set():
        return {'ok': None, 'bytes': 0, 'elapsed': 0.0, 'speed_bps': 0.0, 'status': 0, 'note': 'uygun büyük dosya yok'}
    rf = max(candidates, key=lambda x: x.size)
    session = build_session(); session.headers.update(cfg.headers)
    headers = {'Range': f'bytes=0-{STREAM_PROBE_BYTES-1}', 'Accept': 'application/octet-stream'}
    started = time.perf_counter(); got = 0
    try:
        with session.get(rf.url, headers=headers, auth=cfg.auth, stream=True, timeout=(CONNECT_TIMEOUT, 45), allow_redirects=True) as r:
            if r.status_code in (401,403,404):
                raise RuntimeError(f'HTTP {r.status_code}')
            r.raise_for_status()
            for chunk in r.iter_content(chunk_size=256*1024):
                if stop_event.is_set():
                    break
                if not chunk:
                    continue
                need = STREAM_PROBE_BYTES - got
                got += min(len(chunk), max(0, need))
                if got >= STREAM_PROBE_BYTES:
                    break
            elapsed = max(0.001, time.perf_counter()-started)
            speed = got/elapsed
            ok = got >= min(STREAM_PROBE_BYTES, rf.size) or got >= 1024*1024
            log(f'Gerçek veri akış testi: {human_bytes(got)} • {human_bytes(speed)}/sn • {elapsed:.1f} sn • HTTP {r.status_code}')
            return {'ok': ok, 'bytes': got, 'elapsed': elapsed, 'speed_bps': speed, 'status': int(r.status_code), 'note': rf.rel_path}
    except Exception as e:
        elapsed = max(0.001, time.perf_counter()-started)
        log(f'Gerçek veri akış testi başarısız/yavaş: {type(e).__name__}: {e}')
        return {'ok': False, 'bytes': got, 'elapsed': elapsed, 'speed_bps': got/elapsed if got else 0.0, 'status': 0, 'note': str(e)}
    finally:
        session.close()


def test_recovery_payload_parallel(cfg: EndpointConfig, files: List[RemoteFile], log,
                                   stop_event: threading.Event, levels=(1, 2, 3),
                                   bytes_per_request: int = 1024 * 1024) -> dict:
    """After connectivity returns, gently test 1→2→3 real Range streams.

    This is intentionally small and is only used after an initial connectivity
    outage. It helps distinguish "metadata is back" from "payload streaming is
    actually back" without hammering the server.
    """
    candidates = [f for f in files if f.size >= 16 * 1024 * 1024 and f.rel_path.lower().endswith(('.fq.gz', '.fastq.gz', '.bam', '.cram'))]
    if not candidates:
        candidates = [f for f in files if f.size >= 16 * 1024 * 1024]
    if not candidates:
        return {'stable_max': 1, 'results': [], 'note': 'uygun büyük dosya yok'}

    def one(rf: RemoteFile) -> dict:
        if stop_event.is_set():
            return {'ok': False, 'status': 0, 'bytes': 0, 'elapsed': 0.0, 'error': 'durduruldu'}
        s = build_session(); s.headers.update(cfg.headers)
        got = 0; started = time.perf_counter(); status = 0
        try:
            headers = {'Range': f'bytes=0-{bytes_per_request-1}', 'Accept': 'application/octet-stream', 'Cache-Control': 'no-cache'}
            with s.get(rf.url, headers=headers, auth=cfg.auth, stream=True,
                       timeout=(CONNECT_TIMEOUT, 35), allow_redirects=True) as r:
                status = int(r.status_code)
                if status not in (200, 206):
                    return {'ok': False, 'status': status, 'bytes': 0, 'elapsed': max(.001,time.perf_counter()-started), 'error': f'HTTP {status}'}
                for chunk in r.iter_content(chunk_size=256*1024):
                    if stop_event.is_set(): break
                    if not chunk: continue
                    need = bytes_per_request - got
                    got += min(len(chunk), max(0, need))
                    if got >= bytes_per_request: break
            elapsed=max(.001,time.perf_counter()-started)
            return {'ok': got >= min(bytes_per_request, max(1,rf.size)), 'status': status, 'bytes': got, 'elapsed': elapsed, 'error': ''}
        except Exception as e:
            return {'ok': False, 'status': status, 'bytes': got, 'elapsed': max(.001,time.perf_counter()-started), 'error': f'{type(e).__name__}: {e}'}
        finally:
            s.close()

    results=[]; stable=1
    for level in levels:
        if stop_event.is_set(): break
        chosen=[candidates[i % len(candidates)] for i in range(level)]
        wall=time.perf_counter()
        with ThreadPoolExecutor(max_workers=level, thread_name_prefix='ngsrecovery') as pool:
            rows=list(pool.map(one, chosen))
        elapsed=max(.001,time.perf_counter()-wall)
        success=sum(1 for r in rows if r.get('ok'))
        total_bytes=sum(int(r.get('bytes',0) or 0) for r in rows)
        speed=total_bytes/elapsed
        statuses=','.join(str(r.get('status',0)) for r in rows)
        log(f'Bağlantı dönüş payload testi {level}: {success}/{level} başarılı • {human_bytes(total_bytes)} • {human_bytes(speed)}/sn • HTTP {statuses}')
        results.append({'level':level,'success':success,'speed_bps':speed,'elapsed':elapsed,'statuses':statuses})
        if success == level:
            stable=level
        else:
            break
    return {'stable_max': stable, 'results': results}


COMPARE_SKIP_DIRS = {WORK_DIR_NAME.lower(), PART_DIR_NAME.lower(), REPORT_DIR_NAME.lower()}


def _compare_inventory(root: Path) -> Dict[str, Path]:
    """Return relative files, excluding downloader work folders if encountered."""
    root = Path(root)
    out: Dict[str, Path] = {}
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d.lower() not in COMPARE_SKIP_DIRS]
        b = Path(base)
        for name in files:
            fp = b / name
            try:
                rel = fp.relative_to(root).as_posix()
            except Exception:
                continue
            out[rel] = fp
    return out


def _hash_pair_sha256(left: Path, right: Path, progress_cb=None, stop_event: Optional[threading.Event] = None) -> Tuple[str, str]:
    hl = hashlib.sha256(); hr = hashlib.sha256()
    with left.open('rb') as fl, right.open('rb') as fr:
        while True:
            if stop_event is not None and stop_event.is_set():
                raise RuntimeError('Karşılaştırma durduruldu')
            bl = fl.read(CHUNK_SIZE); br = fr.read(CHUNK_SIZE)
            if bl: hl.update(bl)
            if br: hr.update(br)
            if progress_cb and (bl or br): progress_cb(len(bl) + len(br))
            if not bl and not br: break
    return hl.hexdigest().lower(), hr.hexdigest().lower()


def compare_two_folders(left: Path, right: Path, stop_event: threading.Event, progress_cb=None, item_cb=None) -> dict:
    """Byte-identity comparison using relative path + size + SHA-256."""
    left = Path(left); right = Path(right)
    a = _compare_inventory(left); b = _compare_inventory(right)
    rels = sorted(set(a) | set(b), key=lambda x: x.lower())
    rows = []
    hash_bytes_total = 0
    for rel in rels:
        if rel in a and rel in b:
            try:
                sa = a[rel].stat().st_size; sb = b[rel].stat().st_size
                if sa == sb: hash_bytes_total += sa + sb
            except OSError: pass
    bytes_done = 0; started = time.time()
    counts = {'same':0,'hash_diff':0,'size_diff':0,'only_left':0,'only_right':0,'error':0}
    for idx, rel in enumerate(rels, 1):
        if stop_event.is_set(): raise RuntimeError('Karşılaştırma durduruldu')
        pa = a.get(rel); pb = b.get(rel)
        row = {'file':rel,'status':'','left_size':'','right_size':'','left_sha256':'','right_sha256':'','note':''}
        try:
            if pa is None:
                row['status']='ONLY_HDD'; row['right_size']=pb.stat().st_size; row['note']='Yalnız ikinci klasörde'; counts['only_right'] += 1
            elif pb is None:
                row['status']='ONLY_DOWNLOAD'; row['left_size']=pa.stat().st_size; row['note']='Yalnız ilk klasörde'; counts['only_left'] += 1
            else:
                sa=pa.stat().st_size; sb=pb.stat().st_size; row['left_size']=sa; row['right_size']=sb
                if sa != sb:
                    row['status']='SIZE_DIFFERENT'; row['note']='Dosya boyutu farklı'; counts['size_diff'] += 1
                else:
                    def tick(n):
                        nonlocal bytes_done
                        bytes_done += n
                        if progress_cb: progress_cb(bytes_done, hash_bytes_total, idx-1, len(rels), time.time()-started)
                    ha,hb=_hash_pair_sha256(pa,pb,tick,stop_event)
                    row['left_sha256']=ha; row['right_sha256']=hb
                    if ha == hb:
                        row['status']='IDENTICAL'; row['note']='SHA-256 birebir aynı'; counts['same'] += 1
                    else:
                        row['status']='HASH_DIFFERENT'; row['note']='Aynı boyut, içerik farklı'; counts['hash_diff'] += 1
        except Exception as e:
            if stop_event.is_set(): raise
            row['status']='READ_ERROR'; row['note']=f'{type(e).__name__}: {e}'; counts['error'] += 1
        rows.append(row)
        if item_cb: item_cb(rel,row['status'],row['note'])
        if progress_cb: progress_cb(bytes_done, hash_bytes_total, idx, len(rels), time.time()-started)
    clean = not any(counts[k] for k in ('hash_diff','size_diff','only_left','only_right','error'))
    return {'left':str(left),'right':str(right),'rows':rows,'counts':counts,'total_files':len(rels),'clean':clean,'elapsed':time.time()-started}


def write_compare_report(result: dict, left: Path) -> Path:
    """Tiny comparison report on the external/data volume, never beside Python."""
    stamp=time.strftime('%Y%m%d_%H%M%S')
    base = Path(left).parent / WORK_DIR_NAME / 'Comparisons' / f'{clean_component(Path(left).name)}_{stamp}'
    safe_mkdir(base)
    csv_path=base/'KARSILASTIRMA_DETAY.csv'
    with csv_path.open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=['file','status','left_size','right_size','left_sha256','right_sha256','note']); w.writeheader(); w.writerows(result['rows'])
    c=result['counts']; verdict='BİREBİR AYNI' if result['clean'] else 'FARK BULUNDU'
    text=(
        'NGS SAFE DOWNLOADER - İKİ KLASÖR KARŞILAŞTIRMA\n'
        '================================================\n'
        f'Sonuç: {verdict}\nKlasör 1 (internet): {result["left"]}\nKlasör 2 (fiziksel HDD): {result["right"]}\n'
        f'Dosya birliği: {result["total_files"]}\nBirebir aynı (SHA-256): {c["same"]}\nİçerik farklı: {c["hash_diff"]}\n'
        f'Boyut farklı: {c["size_diff"]}\nYalnız klasör 1: {c["only_left"]}\nYalnız klasör 2: {c["only_right"]}\n'
        f'Okuma hatası: {c["error"]}\nSüre: {format_duration(result.get("elapsed",0))}\n'
        'Yöntem: Önce göreli dosya yolu ve boyut; aynı boyuttaki her eş için SHA-256.\n')
    (base/'KARSILASTIRMA_SONUCU.txt').write_text(text,encoding='utf-8')
    return base


class App(tk.Tk):
    """Transfer, verification/repair, and local disk-to-disk comparison GUI."""
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("1280x880")
        self.minsize(1080, 730)
        self.state = load_app_state()
        self.cfg_obj: Optional[EndpointConfig] = None
        self.files: List[RemoteFile] = []
        self.total_size = 0
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.log_file_path: Optional[Path] = None
        self.log_lock = threading.Lock()
        self.worker_thread: Optional[threading.Thread] = None
        self.ui_queue: queue.Queue = queue.Queue()
        self.active_mode = ''
        self.workflow_mode = ''
        self.verify_tree_items = {}
        self.verify_problem_beeped = False
        self.session_started_at = None
        self.initial_reconnects = 0
        self.file_grid_items = {}
        self.file_grid_files = []
        self.file_grid_dest: Optional[Path] = None
        self.stage_widgets = {}
        self.current_stage = None
        self.last_job_state = load_last_job_state()
        self.auto_resume_pending = False
        self.auto_resume_after_id = None
        self.compare_grid_items = {}
        self.compare_grid_notes = {}
        self.compare_left: Optional[Path] = None
        self.compare_right: Optional[Path] = None
        # v2.5 dashboard state. These are UI-only counters; transfer/verification
        # logic remains unchanged.
        self.error_tree_items = {}
        self.current_problem_paths = set()
        self.repair_pending_paths = set()
        self.repair_repaired_count = 0
        self.dashboard_dataset_total_bytes = 0
        self.dashboard_dataset_files = 0
        self.dashboard_operation_started = None
        self._advanced_visible = False
        self._build_ui()
        self.after(100, self._drain_ui_queue)
        self.after(600, self._restore_last_job_status)
        self.after(1000, self._tick_dashboard_clock)
        self.after(1200, self._refresh_file_grid)

    def _style(self):
        style = ttk.Style(self)
        try:
            style.theme_use('clam')
        except Exception:
            pass
        self.configure(bg='#eef3f7')
        style.configure('App.TFrame', background='#eef3f7')
        style.configure('Hero.TFrame', background='#123a59')
        style.configure('HeroTitle.TLabel', background='#123a59', foreground='white', font=('Segoe UI', 20, 'bold'))
        style.configure('HeroSub.TLabel', background='#123a59', foreground='#cfe1ec', font=('Segoe UI', 9))
        style.configure('HeroStatus.TLabel', background='#1d5278', foreground='white', font=('Segoe UI', 9, 'bold'), padding=(12, 7))
        style.configure('Card.TLabelframe', background='white', padding=11, borderwidth=1, relief='solid')
        style.configure('Card.TLabelframe.Label', background='#eef3f7', foreground='#24475e', font=('Segoe UI', 10, 'bold'))
        style.configure('Auto.TButton', font=('Segoe UI', 11, 'bold'), padding=(18, 13), background='#16689d', foreground='white')
        style.map('Auto.TButton', background=[('active', '#11577f'), ('disabled', '#a8bac7')], foreground=[('disabled', '#f5f7f8')])
        style.configure('Verify.TButton', font=('Segoe UI', 10, 'bold'), padding=(16, 13), background='#e8f1f6', foreground='#234b65')
        style.map('Verify.TButton', background=[('active', '#d7e8f1'), ('disabled', '#e7e7e7')])
        style.configure('Compare.TButton', font=('Segoe UI', 10, 'bold'), padding=(14, 13), background='#f1f4f6', foreground='#445762')
        style.map('Compare.TButton', background=[('active', '#e4e9ec'), ('disabled', '#e7e7e7')])
        style.configure('Action.TButton', padding=(10, 7))
        style.configure('Subtle.TButton', padding=(8, 5), font=('Segoe UI', 8))
        style.configure('NGS.Horizontal.TProgressbar', troughcolor='#dce6ed', background='#1b75ad', lightcolor='#1b75ad', darkcolor='#1b75ad', thickness=14)
        style.configure('Treeview', rowheight=25, font=('Segoe UI', 9), background='white', fieldbackground='white')
        style.configure('Treeview.Heading', font=('Segoe UI', 9, 'bold'), background='#e7eef4', foreground='#24475e')
        style.configure('TNotebook', background='#eef3f7', borderwidth=0)
        style.configure('TNotebook.Tab', padding=(12, 7), font=('Segoe UI', 9))
        style.map('TNotebook.Tab', background=[('selected', 'white')], foreground=[('selected', '#174f72')])

    def _metric_card(self, parent, col: int, key: str, title: str, accent: str, value='—', sub=''):
        card = tk.Frame(parent, bg='white', highlightbackground='#d7e1e7', highlightthickness=1, bd=0)
        card.grid(row=0, column=col, sticky='nsew', padx=(0 if col == 0 else 4, 0 if col == 5 else 4), pady=0)
        parent.columnconfigure(col, weight=1, uniform='metric')
        stripe = tk.Frame(card, bg=accent, height=3)
        stripe.pack(fill='x')
        tk.Label(card, text=title, bg='white', fg='#6b7f8b', font=('Segoe UI', 8, 'bold'), anchor='w').pack(fill='x', padx=10, pady=(8, 0))
        v = tk.StringVar(value=value)
        sv = tk.StringVar(value=sub)
        lbl = tk.Label(card, textvariable=v, bg='white', fg='#1f3d50', font=('Segoe UI', 15, 'bold'), anchor='w')
        lbl.pack(fill='x', padx=10, pady=(1, 0))
        tk.Label(card, textvariable=sv, bg='white', fg='#82929c', font=('Segoe UI', 8), anchor='w').pack(fill='x', padx=10, pady=(0, 8))
        self.metric_vars[key] = v
        self.metric_sub_vars[key] = sv
        self.metric_value_labels[key] = lbl

    def _build_ui(self):
        self._style()
        root = ttk.Frame(self, padding=12, style='App.TFrame')
        root.pack(fill='both', expand=True)

        # Header: deliberately compact so the dashboard receives most of the screen.
        hero = ttk.Frame(root, style='Hero.TFrame', padding=(16, 11))
        hero.pack(fill='x', pady=(0, 8))
        hb = ttk.Frame(hero, style='Hero.TFrame')
        hb.pack(side='left', fill='x', expand=True)
        ttk.Label(hb, text='NGS Safe Downloader', style='HeroTitle.TLabel').pack(anchor='w')
        ttk.Label(hb, text=f'v{APP_VERSION}  •  kesintiye dayanıklı aktarım  •  doğrulama arşivi  •  temiz HDD', style='HeroSub.TLabel').pack(anchor='w', pady=(1,0))
        self.top_status_var = tk.StringVar(value='Hazır')
        ttk.Label(hero, textvariable=self.top_status_var, style='HeroStatus.TLabel').pack(side='right', padx=(12,0))

        # Previous job banner.
        lastjob = tk.Frame(root, bg='#ffffff', highlightbackground='#d7e1e7', highlightthickness=1)
        lastjob.pack(fill='x', pady=(0,8))
        self.last_job_title_var = tk.StringVar(value='Son iş: kayıt yok')
        self.last_job_detail_var = tk.StringVar(value='Yeni bir aktarım başlatabilirsiniz.')
        self.last_job_title_lbl = tk.Label(lastjob, textvariable=self.last_job_title_var, bg='white', fg='#34566d', font=('Segoe UI',9,'bold'), anchor='w')
        self.last_job_title_lbl.pack(side='left', padx=(10,8), pady=7)
        self.last_job_detail_lbl = tk.Label(lastjob, textvariable=self.last_job_detail_var, bg='white', fg='#6c7f8b', font=('Segoe UI',8), anchor='w')
        self.last_job_detail_lbl.pack(side='left', fill='x', expand=True, padx=(0,8), pady=7)
        self.resume_now_btn = ttk.Button(lastjob, text='Şimdi devam et', style='Subtle.TButton', command=self._resume_saved_job_now, state='disabled')
        self.resume_now_btn.pack(side='right', padx=(8,9), pady=5)

        # Source / target. Connection state is visible right beside the share URL.
        source = ttk.LabelFrame(root, text='Kaynak ve hedef', style='Card.TLabelframe')
        source.pack(fill='x', pady=(0,8))
        source.columnconfigure(0, weight=1)
        source.columnconfigure(1, weight=0)
        source.columnconfigure(2, weight=0)
        last_remote = self.state.get('last_remote','')
        last_local = self.state.get('last_local', str(Path.home() / 'Downloads' / 'NGS_Download'))
        self.url_var = tk.StringVar(value=last_remote)
        self.password_var = tk.StringVar()
        self.dest_var = tk.StringVar(value=last_local)

        url_head = tk.Frame(source, bg='white')
        url_head.grid(row=0, column=0, columnspan=2, sticky='ew')
        tk.Label(url_head, text='Paylaşım bağlantısı', bg='white', fg='#344f60', font=('Segoe UI',8,'bold')).pack(side='left')
        self.connection_var = tk.StringVar(value='●  BAĞLI DEĞİL')
        self.connection_lbl = tk.Label(url_head, textvariable=self.connection_var, bg='#eef2f4', fg='#657984', font=('Segoe UI',8,'bold'), padx=8, pady=2)
        self.connection_lbl.pack(side='left', padx=(9,0))
        ttk.Entry(source, textvariable=self.url_var).grid(row=1, column=0, columnspan=2, sticky='ew', padx=(0,8), pady=(3,7))
        ttk.Label(source, text='Parola (varsa)').grid(row=0, column=2, sticky='w')
        ttk.Entry(source, textvariable=self.password_var, show='*', width=22).grid(row=1, column=2, sticky='ew', pady=(3,7))
        ttk.Label(source, text='Hedef klasör / HDD').grid(row=2, column=0, sticky='w')
        ttk.Entry(source, textvariable=self.dest_var).grid(row=3, column=0, columnspan=2, sticky='ew', padx=(0,8), pady=(3,0))
        ttk.Button(source, text='Klasör seç', style='Action.TButton', command=self.choose_dest).grid(row=3, column=2, sticky='ew', pady=(3,0))

        details_bar = tk.Frame(source, bg='white')
        details_bar.grid(row=4, column=0, columnspan=3, sticky='ew', pady=(7,0))
        self.storage_hint_var = tk.StringVar(value='Yarım dosyalar işlem sırasında HDD üzerinde tutulur; doğrulama bitince küçük raporlar program klasörüne arşivlenir ve HDD çalışma klasörü temizlenir.')
        tk.Label(details_bar, textvariable=self.storage_hint_var, bg='white', fg='#71848f', font=('Segoe UI',8), anchor='w').pack(side='left', fill='x', expand=True)
        self.advanced_toggle_btn = ttk.Button(details_bar, text='Yollar ve ayarlar  ▾', style='Subtle.TButton', command=self._toggle_advanced)
        self.advanced_toggle_btn.pack(side='right', padx=(8,0))

        # Collapsed by default: paths and maintenance options are available when needed.
        self.advanced_frame = tk.Frame(source, bg='#f8fafb', highlightbackground='#dce5ea', highlightthickness=1)
        self.advanced_frame.grid(row=5, column=0, columnspan=3, sticky='ew', pady=(8,0))
        self.advanced_frame.columnconfigure(0, weight=1)
        history = tk.Frame(self.advanced_frame, bg='#f8fafb')
        history.grid(row=0, column=0, sticky='ew', padx=9, pady=7)
        history.columnconfigure(0, weight=1)
        self.last_remote_var = tk.StringVar(value=f"Son uzak: {last_remote or '—'}")
        self.last_local_var = tk.StringVar(value=f"Son yerel: {last_local or '—'}")
        self.part_dir_var = tk.StringVar(value=f"Parça klasörü: {Path(last_local).parent / PART_DIR_NAME / _job_key(Path(last_local))}")
        self.record_dir_var = tk.StringVar(value=f'Doğrulama arşivi (program klasörü): {verification_archive_root()}')
        for r, var, color in [(0,self.last_remote_var,'#607785'),(1,self.last_local_var,'#607785'),(2,self.part_dir_var,'#8a6516'),(3,self.record_dir_var,'#607785')]:
            tk.Label(history, textvariable=var, bg='#f8fafb', fg=color, font=('Segoe UI',8), anchor='w', justify='left', wraplength=860).grid(row=r,column=0,sticky='w',pady=1)
        hist_buttons = ttk.Frame(self.advanced_frame)
        hist_buttons.grid(row=0,column=1,sticky='ne',padx=(5,9),pady=7)
        ttk.Button(hist_buttons, text='Parça klasörünü aç', style='Subtle.TButton', command=self.open_part_dir).pack(fill='x', pady=(0,4))
        self.clear_parts_btn = ttk.Button(hist_buttons, text='Mevcut parçaları sil', style='Subtle.TButton', command=self.clear_parts_clicked)
        self.clear_parts_btn.pack(fill='x', pady=(0,4))
        ttk.Button(hist_buttons, text='Doğrulama arşivini aç', style='Subtle.TButton', command=self.open_record_dir).pack(fill='x')

        self.gzip_verify_var = tk.BooleanVar(value=True)
        self.sound_done_var = tk.BooleanVar(value=True)
        self.sound_error_var = tk.BooleanVar(value=True)
        self.resume_parts_var = tk.BooleanVar(value=True)
        self.cleanup_success_var = tk.BooleanVar(value=True)
        opts = tk.Frame(self.advanced_frame, bg='#f8fafb')
        opts.grid(row=1,column=0,columnspan=2,sticky='ew',padx=9,pady=(0,8))
        ttk.Checkbutton(opts, text='Bitişte bip', variable=self.sound_done_var).pack(side='left', padx=(0,12))
        ttk.Checkbutton(opts, text='Sorunda bip', variable=self.sound_error_var).pack(side='left', padx=(0,12))
        tk.Label(opts, text='Doğrulama bitince: raporları program klasörüne taşı • HDD çalışma klasörünü temizle', bg='#f8fafb', fg='#607785', font=('Segoe UI',8)).pack(side='left')
        self.advanced_frame.grid_remove()
        self.dest_var.trace_add('write', lambda *_: (self._update_part_dir_label(), self._update_record_dir_label()))
        self.url_var.trace_add('write', lambda *_: self._set_connection_ui('idle', 'BAĞLI DEĞİL'))
        self._update_record_dir_label()

        # Main actions.
        modes = ttk.Frame(root, style='App.TFrame')
        modes.pack(fill='x', pady=(0,8))
        self.auto_btn = ttk.Button(modes, text='BAŞLAT / DEVAM ET\nİndir → Doğrula → Gerekirse onar → Temiz bitir', style='Auto.TButton', command=self.auto_clicked)
        self.auto_btn.pack(side='left', fill='x', expand=True, padx=(0,5))
        self.verify_btn = ttk.Button(modes, text='DURUMU KONTROL ET\nDosyalara dokunmadan bütünlük taraması', style='Verify.TButton', command=self.verify_only_clicked)
        self.verify_btn.pack(side='left', fill='x', expand=True, padx=5)
        self.compare_btn = ttk.Button(modes, text='İKİ KLASÖRÜ KARŞILAŞTIR\nAğ kopyası ↔ fiziksel HDD', style='Compare.TButton', command=self.compare_clicked)
        self.compare_btn.pack(side='left', fill='x', expand=True, padx=(5,0))

        # --- v2.4 dashboard ---
        dashboard = tk.Frame(root, bg='white', highlightbackground='#d4dfe6', highlightthickness=1)
        dashboard.pack(fill='x', pady=(0,8))
        dash_head = tk.Frame(dashboard, bg='white')
        dash_head.pack(fill='x', padx=12, pady=(9,6))
        self.operation_title_var = tk.StringVar(value='Hazır')
        self.operation_state_var = tk.StringVar(value='Yeni bir aktarım başlatabilirsiniz.')
        tk.Label(dash_head, textvariable=self.operation_title_var, bg='white', fg='#173f59', font=('Segoe UI',12,'bold'), anchor='w').pack(side='left')
        tk.Label(dash_head, textvariable=self.operation_state_var, bg='white', fg='#71848f', font=('Segoe UI',8), anchor='w').pack(side='left', padx=(10,0))
        self.dashboard_pct_var = tk.StringVar(value='0.0%')
        tk.Label(dash_head, textvariable=self.dashboard_pct_var, bg='white', fg='#176b9d', font=('Segoe UI',15,'bold')).pack(side='right')

        self.metric_vars = {}
        self.metric_sub_vars = {}
        self.metric_value_labels = {}
        metrics = tk.Frame(dashboard, bg='white')
        metrics.pack(fill='x', padx=12, pady=(0,8))
        self._metric_card(metrics,0,'speed','İNTERNET HIZI','#3d8dc1','—','Anlık aktarım')
        self._metric_card(metrics,1,'total','TOPLAM','#607d8b','—','Dosya listesi bekleniyor')
        self._metric_card(metrics,2,'done','TAMAM','#2d9a61','0','0 B')
        self._metric_card(metrics,3,'remaining','KALAN','#d18b2c','—','—')
        self._metric_card(metrics,4,'errors','HATALI','#c84c45','0','Doğrulama / indirme')
        self._metric_card(metrics,5,'repair','İŞLENECEK','#9858a6','0','Yeniden indirme kuyruğu')

        middle = tk.Frame(dashboard, bg='white')
        middle.pack(fill='x', padx=12, pady=(0,9))
        mainp = tk.Frame(middle, bg='white')
        mainp.pack(side='left', fill='both', expand=True, padx=(0,10))
        self.dashboard_progress_detail_var = tk.StringVar(value='İşlem başlamadı.')
        self.eta_var = tk.StringVar(value='Tahmini bitiş  —')
        self.elapsed_var = tk.StringVar(value='Geçen süre  —')
        ptop = tk.Frame(mainp, bg='white')
        ptop.pack(fill='x')
        tk.Label(ptop, textvariable=self.dashboard_progress_detail_var, bg='white', fg='#3b596b', font=('Segoe UI',9,'bold'), anchor='w').pack(side='left', fill='x', expand=True)
        tk.Label(ptop, textvariable=self.eta_var, bg='white', fg='#4d6b7b', font=('Segoe UI',9,'bold')).pack(side='right')
        self.progress = ttk.Progressbar(mainp, orient='horizontal', mode='determinate', maximum=100, style='NGS.Horizontal.TProgressbar')
        self.progress.pack(fill='x', pady=(5,4))
        pbottom = tk.Frame(mainp, bg='white')
        pbottom.pack(fill='x')
        self.summary_var = tk.StringVar(value='Hazır.')
        self.progress_var = tk.StringVar(value='')
        tk.Label(pbottom, textvariable=self.summary_var, bg='white', fg='#758892', font=('Segoe UI',8), anchor='w').pack(side='left', fill='x', expand=True)
        tk.Label(pbottom, textvariable=self.elapsed_var, bg='white', fg='#758892', font=('Segoe UI',8)).pack(side='right')

        repair_box = tk.Frame(middle, bg='#f7f9fb', highlightbackground='#d7e1e7', highlightthickness=1, width=300)
        repair_box.pack(side='right', fill='y')
        repair_box.pack_propagate(False)
        self.repair_title_var = tk.StringVar(value='Otomatik onarım')
        self.repair_status_var = tk.StringVar(value='Hatalı dosya yok')
        self.repair_queue_var = tk.StringVar(value='Kuyruk: 0')
        self.repair_done_var = tk.StringVar(value='Yeniden indirilen: 0')
        tk.Label(repair_box, textvariable=self.repair_title_var, bg='#f7f9fb', fg='#3f5260', font=('Segoe UI',9,'bold'), anchor='w').pack(fill='x', padx=10, pady=(8,1))
        tk.Label(repair_box, textvariable=self.repair_status_var, bg='#f7f9fb', fg='#7a4b87', font=('Segoe UI',10,'bold'), anchor='w').pack(fill='x', padx=10)
        rline=tk.Frame(repair_box,bg='#f7f9fb'); rline.pack(fill='x',padx=10,pady=(3,4))
        tk.Label(rline,textvariable=self.repair_queue_var,bg='#f7f9fb',fg='#6f818c',font=('Segoe UI',8)).pack(side='left')
        tk.Label(rline,textvariable=self.repair_done_var,bg='#f7f9fb',fg='#6f818c',font=('Segoe UI',8)).pack(side='right')
        ttk.Button(repair_box,text='Hatalıları göster',style='Subtle.TButton',command=self._show_errors_tab).pack(anchor='e',padx=8,pady=(0,7))

        # Three-stage rail below the dashboard.
        stages = tk.Frame(dashboard, bg='white')
        stages.pack(fill='x', padx=12, pady=(0,10))
        self.stage_widgets = {}
        for key, title in [('download','1  İNDİRME / DEVAM'), ('verify','2  DOĞRULAMA'), ('repair','3  HATALILARI DÜZELT')]:
            box = tk.Frame(stages, bg='#f3f6f8', highlightbackground='#d6e0e6', highlightthickness=1, bd=0)
            box.pack(side='left', fill='x', expand=True, padx=(0 if key=='download' else 4, 0 if key=='repair' else 4))
            title_lbl = tk.Label(box, text=title, bg='#f3f6f8', fg='#455b69', font=('Segoe UI',8,'bold'))
            title_lbl.pack(side='left', padx=(9,6), pady=6)
            detail_lbl = tk.Label(box, text='Bekliyor', bg='#f3f6f8', fg='#6c7f8b', font=('Segoe UI',8), anchor='w')
            detail_lbl.pack(side='left', fill='x', expand=True, padx=(0,7), pady=6)
            self.stage_widgets[key] = (box, title_lbl, detail_lbl)

        controls = ttk.Frame(root, style='App.TFrame')
        controls.pack(fill='x', pady=(0,7))
        self.pause_btn = ttk.Button(controls, text='Duraklat', style='Action.TButton', command=self.pause_clicked, state='disabled')
        self.pause_btn.pack(side='left', padx=(0,7))
        self.stop_btn = ttk.Button(controls, text='Güvenli durdur', style='Action.TButton', command=self.stop_clicked, state='disabled')
        self.stop_btn.pack(side='left')
        self.auto_parallel_var = tk.StringVar(value='Paralellik: otomatik ve adaptif')
        ttk.Label(controls, textvariable=self.auto_parallel_var, foreground='#4a6b7e', font=('Segoe UI',8)).pack(side='right')

        # Compact final result line.
        result = tk.Frame(root, bg='#eef3f7')
        result.pack(fill='x', pady=(0,5))
        self.final_download_var = tk.StringVar(value='—')
        self.final_verify_var = tk.StringVar(value='—')
        self.final_time_var = tk.StringVar(value='—')
        self.final_report_var = tk.StringVar(value='—')
        self.final_result_var = tk.StringVar(value='Durum: Hazır')
        tk.Label(result, textvariable=self.final_result_var, bg='#eef3f7', fg='#34566d', font=('Segoe UI',8,'bold'), anchor='w').pack(side='left', fill='x', expand=True)
        tk.Label(result, textvariable=self.final_report_var, bg='#eef3f7', fg='#7a8c96', font=('Segoe UI',8), anchor='e').pack(side='right', padx=(12,0))

        # Details only when needed.
        notebook = ttk.Notebook(root)
        notebook.pack(fill='both', expand=True)
        self.notebook = notebook
        files_tab = ttk.Frame(notebook, padding=6)
        errors_tab = ttk.Frame(notebook, padding=6)
        verify_tab = ttk.Frame(notebook, padding=6)
        compare_tab = ttk.Frame(notebook, padding=6)
        log_tab = ttk.Frame(notebook, padding=6)
        self.errors_tab = errors_tab
        self.compare_tab = compare_tab
        notebook.add(files_tab, text='Dosya görünümü')
        notebook.add(errors_tab, text='Hatalar / yeniden indirme')
        notebook.add(verify_tab, text='Canlı doğrulama')
        notebook.add(compare_tab, text='Disk karşılaştırma')
        notebook.add(log_tab, text='Canlı log')

        self.file_grid_summary_var = tk.StringVar(value='Henüz dosya listesi yok.')
        ttk.Label(files_tab, textvariable=self.file_grid_summary_var, font=('Segoe UI',9,'bold')).pack(anchor='w', pady=(0,5))
        grid_wrap = ttk.Frame(files_tab); grid_wrap.pack(fill='both', expand=True)
        self.file_canvas = tk.Canvas(grid_wrap, bg='white', highlightthickness=0)
        file_scroll = ttk.Scrollbar(grid_wrap, orient='vertical', command=self.file_canvas.yview)
        self.file_canvas.configure(yscrollcommand=file_scroll.set)
        self.file_canvas.pack(side='left', fill='both', expand=True); file_scroll.pack(side='right', fill='y')
        self.file_grid_frame = tk.Frame(self.file_canvas, bg='white')
        self.file_canvas_window = self.file_canvas.create_window((0,0), window=self.file_grid_frame, anchor='nw')
        self.file_grid_frame.bind('<Configure>', lambda e: self.file_canvas.configure(scrollregion=self.file_canvas.bbox('all')))
        self.file_canvas.bind('<Configure>', lambda e: self.file_canvas.itemconfigure(self.file_canvas_window, width=e.width))

        self.error_summary_var = tk.StringVar(value='Hatalı dosya yok. Otomatik modda bulunan sorunlar burada görünür ve yeniden indirme kuyruğu izlenir.')
        ttk.Label(errors_tab, textvariable=self.error_summary_var, font=('Segoe UI',9,'bold')).pack(anchor='w', pady=(0,5))
        ecols=('status','file','action','note')
        self.error_tree=ttk.Treeview(errors_tab,columns=ecols,show='headings')
        for key,title,width in [('status','Durum',165),('file','Dosya',390),('action','Otomatik işlem',185),('note','Ayrıntı',350)]:
            self.error_tree.heading(key,text=title); self.error_tree.column(key,width=width,stretch=(key in ('file','note')))
        self.error_tree.tag_configure('bad',foreground='#b3261e'); self.error_tree.tag_configure('repair',foreground='#7b4b8b'); self.error_tree.tag_configure('ok',foreground='#19733a')
        esb=ttk.Scrollbar(errors_tab,orient='vertical',command=self.error_tree.yview); self.error_tree.configure(yscrollcommand=esb.set)
        self.error_tree.pack(side='left',fill='both',expand=True); esb.pack(side='right',fill='y')

        cols = ('status','file','size','method','note')
        self.verify_tree = ttk.Treeview(verify_tab, columns=cols, show='headings')
        for key, title, width in [('status','Durum',145),('file','Dosya',350),('size','Boyut',130),('method','Kontrol',150),('note','Not',290)]:
            self.verify_tree.heading(key, text=title); self.verify_tree.column(key, width=width, stretch=(key in ('file','note')))
        self.verify_tree.tag_configure('ok', foreground='#19733a'); self.verify_tree.tag_configure('warn', foreground='#9a6400'); self.verify_tree.tag_configure('bad', foreground='#b3261e'); self.verify_tree.tag_configure('checking', foreground='#315b9b')
        vsb = ttk.Scrollbar(verify_tab, orient='vertical', command=self.verify_tree.yview); self.verify_tree.configure(yscrollcommand=vsb.set)
        self.verify_tree.pack(side='left', fill='both', expand=True); vsb.pack(side='right', fill='y')

        self.compare_summary_var = tk.StringVar(value='İki klasör seçildiğinde dosyalar küçük karelerle gösterilir.')
        ttk.Label(compare_tab, textvariable=self.compare_summary_var, font=('Segoe UI',9,'bold')).pack(anchor='w', pady=(0,5))
        self.compare_paths_var = tk.StringVar(value='')
        ttk.Label(compare_tab, textvariable=self.compare_paths_var, foreground='#607785', wraplength=1100).pack(anchor='w', pady=(0,5))
        cmp_wrap = ttk.Frame(compare_tab); cmp_wrap.pack(fill='both', expand=True)
        self.compare_canvas = tk.Canvas(cmp_wrap, bg='white', highlightthickness=0)
        cmp_scroll = ttk.Scrollbar(cmp_wrap, orient='vertical', command=self.compare_canvas.yview); self.compare_canvas.configure(yscrollcommand=cmp_scroll.set)
        self.compare_canvas.pack(side='left', fill='both', expand=True); cmp_scroll.pack(side='right', fill='y')
        self.compare_grid_frame = tk.Frame(self.compare_canvas, bg='white')
        self.compare_canvas_window = self.compare_canvas.create_window((0,0), window=self.compare_grid_frame, anchor='nw')
        self.compare_grid_frame.bind('<Configure>', lambda e: self.compare_canvas.configure(scrollregion=self.compare_canvas.bbox('all')))
        self.compare_canvas.bind('<Configure>', lambda e: self.compare_canvas.itemconfigure(self.compare_canvas_window, width=e.width))
        ttk.Label(compare_tab, text='Yeşil: birebir aynı • Kırmızı: farklı • Sarı: yalnız bir klasörde • Gri: bekliyor', foreground='#607785').pack(anchor='w', pady=(5,0))

        self.log_text = tk.Text(log_tab, height=11, wrap='word', font=('Consolas', 9), relief='flat', bg='#fbfcfd', fg='#263e4c')
        lsb = ttk.Scrollbar(log_tab, command=self.log_text.yview); self.log_text.config(yscrollcommand=lsb.set)
        self.log_text.pack(side='left', fill='both', expand=True); lsb.pack(side='right', fill='y')

        ttk.Label(root, text=f'Veri klasörü saf tutulur • çalışma kayıtları {WORK_DIR_NAME} • yarım dosyalar {PART_DIR_NAME} • büyük dosyalar program diskine yazılmaz.', wraplength=1100, foreground='#71848f', font=('Segoe UI',8)).pack(anchor='w', pady=(5,0))

    def _toggle_advanced(self):
        self._advanced_visible = not self._advanced_visible
        if self._advanced_visible:
            self.advanced_frame.grid()
            self.advanced_toggle_btn.config(text='Yollar ve ayarlar  ▴')
        else:
            self.advanced_frame.grid_remove()
            self.advanced_toggle_btn.config(text='Yollar ve ayarlar  ▾')

    def _set_connection_ui(self, state: str, text: str = ''):
        palette = {
            'idle': ('#eef2f4','#657984'),
            'checking': ('#e8f2f8','#1a648f'),
            'connected': ('#e9f6ee','#18703c'),
            'waiting': ('#fff3dd','#8a5c0b'),
            'error': ('#fdeceb','#a43a32'),
        }
        bg, fg = palette.get(state, palette['idle'])
        label = text or {'idle':'BAĞLI DEĞİL','checking':'BAĞLANIYOR','connected':'BAĞLI','waiting':'BEKLENİYOR','error':'HATA'}.get(state,'')
        self.connection_var.set('●  ' + label)
        try: self.connection_lbl.config(bg=bg, fg=fg)
        except Exception: pass

    def _show_errors_tab(self):
        try: self.notebook.select(self.errors_tab)
        except Exception: pass

    def _reset_dashboard(self, operation='Hazırlanıyor'):
        self.dashboard_operation_started = time.time()
        self.operation_title_var.set(operation)
        self.operation_state_var.set('Bağlantı ve dosya durumu hazırlanıyor...')
        self.dashboard_pct_var.set('0.0%')
        self.dashboard_progress_detail_var.set('Hazırlanıyor...')
        self.eta_var.set('Tahmini bitiş  —')
        self.elapsed_var.set('Geçen süre  00:00')
        self.metric_vars['speed'].set('—'); self.metric_sub_vars['speed'].set('Bağlantı hazırlanıyor')
        self.metric_vars['errors'].set('0'); self.metric_vars['repair'].set('0')
        self.metric_sub_vars['errors'].set('Doğrulama / indirme'); self.metric_sub_vars['repair'].set('Yeniden indirme kuyruğu')
        self.repair_status_var.set('Hatalı dosya yok'); self.repair_queue_var.set('Kuyruk: 0'); self.repair_done_var.set('Yeniden indirilen: 0')
        self.current_problem_paths.clear(); self.repair_pending_paths.clear(); self.repair_repaired_count=0
        for item in self.error_tree.get_children(): self.error_tree.delete(item)
        self.error_tree_items={}
        self.error_summary_var.set('Hatalı dosya yok. Otomatik modda bulunan sorunlar burada görünür ve yeniden indirme kuyruğu izlenir.')

    def _tick_dashboard_clock(self):
        try:
            if self.dashboard_operation_started and self.active_mode:
                self.elapsed_var.set('Geçen süre  ' + format_duration(time.time()-self.dashboard_operation_started))
        except Exception:
            pass
        self.after(1000, self._tick_dashboard_clock)

    def _update_error_dashboard(self):
        n=len(self.current_problem_paths)
        q=len(self.repair_pending_paths)
        self.metric_vars['errors'].set(str(n))
        self.metric_vars['repair'].set(str(q))
        self.repair_queue_var.set(f'Kuyruk: {q}')
        self.repair_done_var.set(f'Yeniden indirilen: {self.repair_repaired_count}')
        if q:
            self.repair_status_var.set(f'{q} dosya işlenecek')
        elif n:
            self.repair_status_var.set(f'{n} sorun bulundu')
        else:
            self.repair_status_var.set('Hatalı dosya yok')
        self.error_summary_var.set(f'Hatalı: {n} • yeniden indirme kuyruğu: {q} • başarılı yeniden indirme: {self.repair_repaired_count}')

    def post(self, kind: str, *args):
        self.ui_queue.put((kind, args))

    def _sound(self, kind='done'):
        try:
            import winsound
            if kind == 'error':
                for hz in (650, 450, 650):
                    winsound.Beep(hz, 180); time.sleep(0.07)
            elif kind == 'server':
                winsound.Beep(520, 220); time.sleep(0.08); winsound.Beep(390, 320)
            elif kind == 'recovered':
                winsound.Beep(740, 140); time.sleep(0.05); winsound.Beep(988, 170)
            else:
                winsound.Beep(880, 140); time.sleep(0.06); winsound.Beep(1175, 180)
        except Exception:
            try: self.bell()
            except Exception: pass

    def _reset_stages(self):
        self.current_stage = None
        for key in ('download','verify','repair'):
            self._set_stage_ui(key, 'idle', 'Bekliyor')

    def _set_stage_ui(self, key: str, state: str, detail: str):
        item = self.stage_widgets.get(key)
        if not item:
            return
        palette = {
            'idle': ('#f3f6f8','#455b69','#6c7f8b'),
            'active': ('#e7f2f9','#155f8b','#155f8b'),
            'ok': ('#eaf7ef','#176b3a','#176b3a'),
            'warn': ('#fff5df','#8a5b05','#8a5b05'),
            'error': ('#fdeceb','#a43a32','#a43a32'),
        }
        bg, fg, dfg = palette.get(state, palette['idle'])
        box, title_lbl, detail_lbl = item
        box.config(bg=bg, highlightbackground=fg)
        title_lbl.config(bg=bg, fg=fg)
        detail_lbl.config(bg=bg, fg=dfg, text=detail)
        if state == 'active':
            self.current_stage = key
        elif self.current_stage == key and state != 'active':
            self.current_stage = None

    def _archive_and_cleanup_verification_records(self, dest: Path, ended: float, full_verified: bool) -> Optional[Path]:
        """Archive small final records beside the app, then clean transient HDD folders."""
        source = Path(dest).parent / WORK_DIR_NAME / _job_key(Path(dest))
        archive = verification_archive_dir(dest, ended, create=True)
        try:
            self.log(f'Doğrulama kayıtları program klasörüne arşivleniyor: {archive}')
            if source.exists():
                for src in [x for x in source.rglob('*') if x.is_file()]:
                    rel = src.relative_to(source)
                    dst = archive / rel
                    safe_mkdir(dst.parent)
                    shutil.copy2(src, dst)
            # Update archived status/report pointers so they do not refer to deleted HDD work paths.
            status = archive / 'NGS_DOWNLOAD_STATUS.json'
            if status.exists():
                try:
                    data = json.loads(status.read_text(encoding='utf-8'))
                    data['verification_archive'] = str(archive)
                    report = archive / 'DOGRULAMA_SONUCU.txt'
                    if report.exists(): data['verification_report'] = str(report)
                    status.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
                except Exception:
                    pass
            self.log_file_path = None
            if source.exists():
                shutil.rmtree(source)
            _prune_empty_root(Path(dest).parent / WORK_DIR_NAME)
            if full_verified:
                # A fully verified dataset never needs stale partials.
                pd = Path(dest).parent / PART_DIR_NAME / _job_key(Path(dest))
                if pd.exists():
                    shutil.rmtree(pd)
                _prune_empty_root(Path(dest).parent / PART_DIR_NAME)
            self.post('final_report_only', str(archive))
            self.post('record_path_refresh')
            self.post('summary', f'Doğrulama kayıtları arşivlendi • HDD çalışma klasörü temizlendi')
            return archive
        except Exception as e:
            self.log_file_path = None
            self.post('summary', f'Doğrulama bitti; kayıt arşivi/temizliği tamamlanamadı: {e}')
            return archive if archive.exists() else None

    def _cleanup_records_after_success(self, dest: Path):
        # Backward-compatible no-op wrapper; v2.6 finalizes records automatically.
        return

    def _set_busy(self, mode: str):
        self.workflow_mode = mode
        self.active_mode = mode
        self.auto_btn.config(state='disabled')
        self.verify_btn.config(state='disabled')
        try: self.compare_btn.config(state='disabled')
        except Exception: pass
        try: self.clear_parts_btn.config(state='disabled')
        except Exception: pass
        self.stop_btn.config(state='normal')
        self.pause_btn.config(state='normal' if mode == 'download' else 'disabled', text='Duraklat')

    def _set_idle(self):
        self.active_mode = ''
        self.workflow_mode = ''
        self.auto_btn.config(state='normal')
        self.verify_btn.config(state='normal')
        try: self.compare_btn.config(state='normal')
        except Exception: pass
        try: self.clear_parts_btn.config(state='normal')
        except Exception: pass
        self.stop_btn.config(state='disabled')
        self.pause_btn.config(state='disabled', text='Duraklat')

    def _clear_verify_tree(self):
        for item in self.verify_tree.get_children(): self.verify_tree.delete(item)
        self.verify_tree_items = {}
        self.verify_problem_beeped = False
        # Each verification pass starts with a fresh active-problem set. The
        # error-history table remains so repaired files can turn green.
        self.current_problem_paths.clear()
        self._update_error_dashboard()

    def _upsert_error_item(self, rel: str, status: str, action: str, note: str, tag='bad'):
        values=(status, rel, action, note)
        item=self.error_tree_items.get(rel)
        if item and self.error_tree.exists(item):
            self.error_tree.item(item, values=values, tags=(tag,))
        else:
            item=self.error_tree.insert('', 'end', values=values, tags=(tag,))
            self.error_tree_items[rel]=item
        try: self.error_tree.see(item)
        except Exception: pass

    def _insert_verify_item(self, status, rel, local_size, expected_size, method, note):
        good = {'VERIFIED_HASH','VERIFIED_HASH_AND_GZIP','GZIP_OK_SIZE_OK','SIZE_OK_NO_SOURCE_HASH'}
        warn = {'PARTIAL','MISSING','CHECKING'}
        bad = {'SIZE_MISMATCH','CHECKSUM_MISMATCH','CHECKSUM_AND_GZIP_ERROR','GZIP_ERROR','VENDOR_CHECKSUM_UNREADABLE','VERIFY_ERROR'}
        problem = bad | {'PARTIAL','MISSING'}
        tag = 'ok' if status in good else 'bad' if status in bad else 'checking' if status == 'CHECKING' else 'warn'
        if status in problem and not self.verify_problem_beeped and self.sound_error_var.get():
            self.verify_problem_beeped = True
            threading.Thread(target=self._sound, args=('error',), daemon=True).start()
        size_txt = f"{human_bytes(local_size)} / {human_bytes(expected_size)}" if expected_size else human_bytes(local_size)
        values = (status, rel, size_txt, method, note)
        item = self.verify_tree_items.get(rel)
        if item and self.verify_tree.exists(item):
            self.verify_tree.item(item, values=values, tags=(tag,)); self.verify_tree.see(item)
        else:
            item = self.verify_tree.insert('', 'end', values=values, tags=(tag,)); self.verify_tree_items[rel] = item
            children = self.verify_tree.get_children()
            if len(children) > 500:
                old = children[0]; vals = self.verify_tree.item(old, 'values')
                if vals and len(vals) > 1: self.verify_tree_items.pop(vals[1], None)
                self.verify_tree.delete(old)
            self.verify_tree.yview_moveto(1.0)

        if status in problem:
            self.current_problem_paths.add(rel)
            auto_action = 'Yeniden indirilecek' if self.workflow_mode == 'auto' else 'İncele'
            if status == 'VENDOR_CHECKSUM_UNREADABLE': auto_action = 'Checksum dosyası yeniden alınacak'
            elif status == 'PARTIAL': auto_action = 'Kaldığı yerden devam edecek'
            elif status == 'MISSING': auto_action = 'İndirilecek'
            self._upsert_error_item(rel, status, auto_action, note, 'bad')
        elif status in good:
            self.current_problem_paths.discard(rel)
            if rel in self.error_tree_items:
                self._upsert_error_item(rel, 'SAĞLAM', 'Doğrulandı', 'Son kontrolde sorun yok', 'ok')
        self._update_error_dashboard()

    def _handle_repair_queue(self, paths, round_no=0, max_rounds=0, repaired_count=0, phase=''):
        new=set(paths or [])
        prev=set(self.repair_pending_paths)
        resolved=prev-new
        self.repair_pending_paths=new
        self.repair_repaired_count=max(self.repair_repaired_count, int(repaired_count or 0))
        for rel in resolved:
            self.current_problem_paths.discard(rel)
            self._upsert_error_item(rel, 'YENİDEN İNDİRİLDİ', 'Doğrulama bekliyor', 'Aktarım tamamlandı; bütünlük kontrolünde tekrar değerlendirilecek', 'repair')
        for rel in sorted(new):
            self.current_problem_paths.add(rel)
            self._upsert_error_item(rel, 'YENİDEN İNDİRME', f'Tur {round_no}/{max_rounds}' if round_no else 'Kuyrukta', phase or 'Otomatik onarım', 'repair')
        if new:
            self.repair_status_var.set(f'{len(new)} dosya yeniden indirilecek')
            self.repair_title_var.set('Otomatik onarım aktif')
        elif self.repair_repaired_count:
            self.repair_status_var.set('Yeniden indirme tamamlandı')
            self.repair_title_var.set('Otomatik onarım')
        self._update_error_dashboard()

    def _drain_ui_queue(self):
        try:
            while True:
                kind, args = self.ui_queue.get_nowait()
                if kind == 'log':
                    self.log_text.insert('end', f"[{time.strftime('%H:%M:%S')}] {args[0]}\n"); self.log_text.see('end')
                elif kind == 'summary':
                    text=args[0]
                    self.summary_var.set(text)
                    self.operation_state_var.set(text)
                    # Network-health messages also drive the compact status chip.
                    low=text.lower()
                    if 'bağlantı tanınıyor' in low or 'sunucu ve dosyalar taranıyor' in low:
                        self._set_connection_ui('checking','BAĞLANIYOR')
                    elif 'ağ/sunucu bekleniyor' in low or 'sunucu bekleme modu' in low or 'sunucu hâlâ sorunlu' in low:
                        self._set_connection_ui('waiting','BEKLENİYOR')
                    elif 'bağlantı geri geldi' in low or 'sunucu yeniden erişilebilir' in low:
                        self._set_connection_ui('connected','BAĞLI')
                    self.top_status_var.set(text[:70] + ('…' if len(text)>70 else ''))
                elif kind == 'connection':
                    self._set_connection_ui(*args)
                elif kind == 'stage':
                    key,state,detail=args
                    self._set_stage_ui(key,state,detail)
                    if state == 'active':
                        names={'download':'İndiriliyor / devam ediyor','verify':'Bütünlük doğrulanıyor','repair':'Hatalılar yeniden indiriliyor'}
                        self.operation_title_var.set(names.get(key,'Çalışıyor'))
                    elif state == 'error':
                        self.operation_title_var.set('Dikkat gerekiyor')
                elif kind == 'repair_queue':
                    self._handle_repair_queue(*args)
                elif kind == 'final_report_only':
                    self.final_report_var.set(args[0])
                elif kind == 'record_path_refresh':
                    self._update_record_dir_label()
                elif kind == 'job_banner':
                    self._set_last_job_banner(*args)
                elif kind == 'parallel':
                    stable, used, ceiling = args
                    m = f'≥{stable}' if ceiling else str(stable)
                    self.auto_parallel_var.set(f'Stabil test: {m} bağlantı • kullanılan: {used}')
                    self.metric_sub_vars['speed'].set(f'{used} aktif bağlantı')
                elif kind == 'progress':
                    done, total, files_done, files_total, speed, eta = args
                    pct = (done / total * 100) if total else 0
                    self.progress['value'] = min(100, pct)
                    speed_txt = f'{human_bytes(speed)}/sn' if speed and speed > 0 else '—'
                    finish = time.strftime('%H:%M', time.localtime(time.time()+eta)) if eta and eta > 0 and eta < 10**8 else '—'
                    self.metric_vars['speed'].set(speed_txt)
                    if self.current_stage == 'repair':
                        self.dashboard_pct_var.set(f'{pct:.1f}%')
                        self.dashboard_progress_detail_var.set(f'Yeniden indirme • {files_done}/{files_total} dosya • {human_bytes(done)} / {human_bytes(total)}')
                    else:
                        self.dashboard_pct_var.set(f'{pct:.1f}%')
                        self.dashboard_progress_detail_var.set(f'İndirme • {files_done}/{files_total} dosya • {human_bytes(done)} / {human_bytes(total)}')
                    self.eta_var.set(f'Tahmini bitiş  {finish}  •  kalan ~{format_duration(eta)}' if finish != '—' else 'Tahmini bitiş  —')
                    self.progress_var.set(f'{pct:5.1f}% • {human_bytes(done)} / {human_bytes(total)} • {speed_txt} • dosya {files_done}/{files_total}')
                    if self.current_stage in ('download','repair'):
                        self._set_stage_ui(self.current_stage, 'active', f'{files_done}/{files_total} • %{pct:.1f} • {speed_txt}')
                elif kind == 'verify_progress':
                    fd, ft, bd, bt, elapsed = args
                    pct = (bd / bt * 100) if bt else ((fd / ft * 100) if ft else 0)
                    self.progress['value'] = min(100, pct)
                    rate = bd / elapsed if elapsed > 0 else 0
                    eta = ((bt-bd)/rate) if rate > 0 and bt > bd else 0
                    finish = time.strftime('%H:%M', time.localtime(time.time()+eta)) if eta > 0 else '—'
                    self.dashboard_pct_var.set(f'{pct:.1f}%')
                    self.dashboard_progress_detail_var.set(f'Doğrulama • ~{fd}/{ft} dosya • {human_bytes(bd)} / {human_bytes(bt)} • disk {human_bytes(rate)}/sn')
                    self.eta_var.set(f'Tahmini bitiş  {finish}  •  kalan ~{format_duration(eta)}' if finish != '—' else 'Tahmini bitiş  —')
                    self.metric_vars['speed'].set('—'); self.metric_sub_vars['speed'].set('Ağ kullanılmıyor • doğrulama')
                    self.progress_var.set(f'Doğrulama {pct:5.1f}% • ~{fd}/{ft} dosya • {human_bytes(rate)}/sn')
                    self._set_stage_ui('verify', 'active', f'~{fd}/{ft} • %{pct:.1f}')
                elif kind == 'compare_init':
                    self._init_compare_grid(*args)
                elif kind == 'compare_item':
                    self._update_compare_grid_item(*args)
                elif kind == 'compare_progress':
                    bd, bt, fd, ft, elapsed = args
                    pct=(bd/bt*100.0) if bt else ((fd/ft*100.0) if ft else 0.0)
                    self.progress['value']=min(100,pct)
                    rate=bd/elapsed if elapsed>0 else 0
                    eta=((bt-bd)/rate) if rate>0 and bt>bd else 0
                    finish=time.strftime('%H:%M',time.localtime(time.time()+eta)) if eta>0 else '—'
                    self.dashboard_pct_var.set(f'{pct:.1f}%')
                    self.dashboard_progress_detail_var.set(f'Disk karşılaştırma • {fd}/{ft} dosya • SHA-256 okuma {human_bytes(rate)}/sn')
                    self.eta_var.set(f'Tahmini bitiş  {finish}  •  kalan ~{format_duration(eta)}' if finish!='—' else 'Tahmini bitiş  —')
                    self.metric_vars['speed'].set('—'); self.metric_sub_vars['speed'].set('Ağ kullanılmıyor • disk karşılaştırma')
                    self.operation_title_var.set('Diskler karşılaştırılıyor')
                    self.top_status_var.set(f'Karşılaştırılıyor {fd}/{ft}')
                elif kind == 'compare_done':
                    summary, report_path, clean = args
                    self.compare_summary_var.set(summary)
                    self.final_result_var.set(('Sonuç: BİREBİR AYNI • ' if clean else 'Sonuç: FARK BULUNDU • ') + summary)
                    self.final_report_var.set(str(report_path))
                    self.top_status_var.set('Karşılaştırma tamamlandı')
                    self.operation_title_var.set('Karşılaştırma tamamlandı' if clean else 'Karşılaştırmada fark bulundu')
                    self.operation_state_var.set(summary)
                    self.dashboard_pct_var.set('100.0%')
                    self.progress['value']=100
                    if clean and self.sound_done_var.get(): threading.Thread(target=self._sound, args=('done',), daemon=True).start()
                    elif (not clean) and self.sound_error_var.get(): threading.Thread(target=self._sound, args=('error',), daemon=True).start()
                elif kind == 'verify_item':
                    self._insert_verify_item(*args)
                elif kind == 'file_grid_init':
                    self._init_file_grid(*args)
                elif kind == 'clear_verify':
                    self._clear_verify_tree()
                elif kind == 'mode':
                    self.active_mode = args[0]
                    self.pause_btn.config(state='normal' if args[0]=='download' else 'disabled')
                elif kind == 'final':
                    dl, vr, tm, rp = args
                    self.final_download_var.set(dl); self.final_verify_var.set(vr); self.final_time_var.set(tm); self.final_report_var.set(rp)
                    self.final_result_var.set(f'Sonuç: {vr}  •  {tm}')
                    clean='TAM DOĞRULANDI' in vr or ('sorun 0' in vr.lower())
                    self.operation_title_var.set('TAMAMLANDI' if clean else 'İşlem tamamlandı • kontrol gerekli')
                    self.operation_state_var.set(vr)
                    if clean:
                        self.dashboard_pct_var.set('100.0%'); self.progress['value']=100
                        self.metric_vars['errors'].set('0'); self.metric_vars['repair'].set('0')
                        self.repair_pending_paths.clear(); self.current_problem_paths.clear(); self._update_error_dashboard()
                    self.eta_var.set('Bitiş  ' + tm.split('Bitiş ',1)[-1].split(' •',1)[0] if 'Bitiş ' in tm else 'İşlem tamamlandı')
                elif kind == 'idle':
                    self._set_idle()
                elif kind == 'error':
                    self._set_idle(); self._set_connection_ui('error','HATA')
                    self.operation_title_var.set('İşlem kesildi')
                    self.operation_state_var.set(args[0])
                    if self.sound_error_var.get(): threading.Thread(target=self._sound, args=('error',), daemon=True).start()
                    messagebox.showerror(APP_NAME, args[0])
                elif kind == 'info':
                    messagebox.showinfo(APP_NAME, args[0])
                elif kind == 'sound':
                    threading.Thread(target=self._sound, args=(args[0],), daemon=True).start()
        except queue.Empty:
            pass
        self.after(100, self._drain_ui_queue)

    def log(self, msg: str):
        self.post('log', msg)
        if self.log_file_path:
            try:
                with self.log_lock:
                    with self.log_file_path.open('a', encoding='utf-8') as f:
                        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
            except Exception: pass

    def _init_compare_grid(self, rels, left_text: str, right_text: str):
        self.compare_left=Path(left_text); self.compare_right=Path(right_text)
        self.compare_grid_items={}; self.compare_grid_notes={}
        for w in self.compare_grid_frame.winfo_children(): w.destroy()
        self.compare_paths_var.set(f'1: {left_text}    ↔    2: {right_text}')
        self.compare_summary_var.set(f'Taranıyor • {len(rels)} dosya')
        cols=max(12, min(42, int(max(600,self.compare_canvas.winfo_width())/22)))
        for i,rel in enumerate(rels):
            lbl=tk.Label(self.compare_grid_frame, text='', width=2, height=1, bg='#d9e1e6', relief='flat', cursor='hand2')
            lbl.grid(row=i//cols,column=i%cols,padx=2,pady=2,sticky='nsew')
            lbl.bind('<Button-1>', lambda e, r=rel: self._show_compare_detail(r))
            self.compare_grid_items[rel]=lbl; self.compare_grid_notes[rel]=('PENDING','Bekliyor')
        try: self.notebook.select(self.compare_tab)
        except Exception: pass

    def _update_compare_grid_item(self, rel: str, status: str, note: str):
        lbl=self.compare_grid_items.get(rel); self.compare_grid_notes[rel]=(status,note)
        if not lbl: return
        colors={'IDENTICAL':'#55b978','HASH_DIFFERENT':'#d9534f','SIZE_DIFFERENT':'#d9534f','ONLY_DOWNLOAD':'#f0b84f','ONLY_HDD':'#f0b84f','READ_ERROR':'#9e3b36','PENDING':'#d9e1e6'}
        lbl.config(bg=colors.get(status,'#d9e1e6'))

    def _show_compare_detail(self, rel: str):
        status,note=self.compare_grid_notes.get(rel,('',''))
        l=(self.compare_left/rel) if self.compare_left else rel; r=(self.compare_right/rel) if self.compare_right else rel
        messagebox.showinfo('Karşılaştırma ayrıntısı', f'Dosya: {rel}\nDurum: {status}\n{note}\n\nKlasör 1: {l}\nKlasör 2: {r}')

    def compare_clicked(self):
        if self.worker_thread and self.worker_thread.is_alive(): return
        initial=self.dest_var.get().strip() or str(Path.home())
        left=filedialog.askdirectory(title='1/2 - İnternetten indirilen klasörü seçin', initialdir=initial)
        if not left: return
        right=filedialog.askdirectory(title='2/2 - Fiziksel HDD ile gelen klasörü seçin', initialdir=str(Path(left).parent))
        if not right: return
        try:
            if Path(left).resolve() == Path(right).resolve(): messagebox.showwarning(APP_NAME,'Aynı klasörü iki kez seçtiniz.'); return
        except Exception: pass
        self.stop_event.clear(); self.pause_event.clear(); self._reset_stages(); self.progress['value']=0; self.progress_var.set('Karşılaştırma hazırlanıyor...')
        self._reset_dashboard('Disk karşılaştırması')
        self.operation_state_var.set('İki klasörün dosya listeleri ve SHA-256 değerleri karşılaştırılacak.')
        self.metric_vars['speed'].set('—'); self.metric_sub_vars['speed'].set('Ağ kullanılmıyor')
        self._set_busy('compare'); self.stop_btn.config(state='normal'); self.pause_btn.config(state='disabled')
        self.worker_thread=threading.Thread(target=self._compare_worker,args=(Path(left),Path(right)),daemon=True); self.worker_thread.start()

    def _compare_worker(self, left: Path, right: Path):
        try:
            self.log(f'İKİ KLASÖR KARŞILAŞTIR başladı: {left} ↔ {right}')
            a=_compare_inventory(left); b=_compare_inventory(right); rels=sorted(set(a)|set(b),key=lambda x:x.lower())
            self.post('compare_init',rels,str(left),str(right))
            result=compare_two_folders(left,right,self.stop_event,
                progress_cb=lambda bd,bt,fd,ft,el:self.post('compare_progress',bd,bt,fd,ft,el),
                item_cb=lambda rel,st,note:self.post('compare_item',rel,st,note))
            report=write_compare_report(result,left); c=result['counts']
            summary=(f'Aynı {c["same"]} • içerik farklı {c["hash_diff"]} • boyut farklı {c["size_diff"]} • '
                     f'yalnız 1: {c["only_left"]} • yalnız 2: {c["only_right"]} • hata {c["error"]} • süre {format_duration(result["elapsed"])}')
            self.post('compare_done',summary,str(report),bool(result['clean'])); self.log(('BİREBİR AYNI' if result['clean'] else 'FARK BULUNDU') + ' • ' + summary)
        except Exception as e:
            if self.stop_event.is_set(): self.post('summary','Karşılaştırma güvenli biçimde durduruldu.')
            else: self.log(traceback.format_exc()); self.post('error',f'Karşılaştırma tamamlanamadı:\n{e}')
        finally: self.post('idle')

    def choose_dest(self):
        d = filedialog.askdirectory(initialdir=self.dest_var.get() or str(Path.home()))
        if d: self.dest_var.set(d)

    def _update_part_dir_label(self):
        try:
            d = Path(self.dest_var.get()).expanduser()
            pd = d.parent / PART_DIR_NAME / _job_key(d)
            self.part_dir_var.set(f"Parça klasörü: {pd}")
        except Exception:
            pass

    def _update_record_dir_label(self):
        try:
            d = Path(self.dest_var.get()).expanduser()
            rd = latest_verification_archive(d)
            self.record_dir_var.set(f"Doğrulama arşivi (program klasörü): {rd}")
        except Exception:
            pass

    def _save_current_state(self):
        data = {
            'last_remote': self.url_var.get().strip(),
            'last_local': self.dest_var.get().strip(),
            'resume_parts': True,
            'cleanup_success': True,
            'gzip_verify': True,
        }
        save_app_state(data)
        self.state = data
        self.last_remote_var.set(f"Son uzak: {data['last_remote'] or '—'}")
        self.last_local_var.set(f"Son yerel: {data['last_local'] or '—'}")
        self._update_part_dir_label()
        self._update_record_dir_label()

    def open_record_dir(self):
        try:
            d = Path(self.dest_var.get()).expanduser()
            rd = latest_verification_archive(d)
            if os.name == 'nt':
                os.startfile(str(rd))
            else:
                self.post('info', str(rd))
        except Exception as e:
            messagebox.showerror(APP_NAME, f'Doğrulama arşivi açılamadı\n{e}')

    def open_part_dir(self):
        try:
            d = part_dir(Path(self.dest_var.get()).expanduser())
            if os.name == 'nt':
                os.startfile(str(d))
            else:
                self.post('info', str(d))
        except Exception as e:
            messagebox.showerror(APP_NAME, f'Parça klasörü açılamadı\n{e}')

    def clear_parts_clicked(self):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning(APP_NAME, 'Aktif indirme varken parça dosyaları silinemez. Önce güvenli durdurun.')
            return
        dest = Path(self.dest_var.get()).expanduser()
        if not messagebox.askyesno(APP_NAME, 'Mevcut yarım indirmeler silinsin mi?\n\nBu işlem yalnız .part dosyalarını siler; tamamlanmış dosyalara dokunmaz. Silinen yarım dosyalar baştan indirilecektir.'):
            return
        count=0; total=0
        # Current external part tree + old in-target part tree.
        for pd in (part_dir(dest), dest / PART_DIR_NAME):
            if pd.exists():
                for pp in list(pd.rglob('*.part')):
                    try:
                        n=pp.stat().st_size; pp.unlink(); count+=1; total+=n
                    except OSError:
                        pass
                try:
                    for d in sorted([x for x in pd.rglob('*') if x.is_dir()], key=lambda x: len(x.parts), reverse=True):
                        try: d.rmdir()
                        except OSError: pass
                    try: pd.rmdir()
                    except OSError: pass
                except OSError: pass
        # Legacy beside-file parts from v1.0-v1.7.
        if dest.exists():
            for pp in dest.rglob('*.part'):
                try:
                    if PART_DIR_NAME in pp.parts or REPORT_DIR_NAME in pp.parts:
                        continue
                    n=pp.stat().st_size; pp.unlink(); count+=1; total+=n
                except OSError:
                    pass
        self.log(f'Kullanıcı parça temizliği: {count} dosya • {human_bytes(total)} silindi.')
        self.file_grid_summary_var.set(f'Parça temizliği tamamlandı • {count} parça • {human_bytes(total)}')
        self._refresh_file_grid(force=True)

    def _init_file_grid(self, items, dest_text: str):
        for w in self.file_grid_frame.winfo_children():
            w.destroy()
        self.file_grid_items = {}
        self.file_grid_files = list(items)
        self.file_grid_dest = Path(dest_text)
        self.dashboard_dataset_files = len(self.file_grid_files)
        self.dashboard_dataset_total_bytes = sum(int(sz or 0) for _, sz in self.file_grid_files)
        self.metric_vars['total'].set(human_bytes(self.dashboard_dataset_total_bytes))
        self.metric_sub_vars['total'].set(f'{self.dashboard_dataset_files} dosya')

        # HDD taramasi / defrag gorunumu: isim yazmayan kucuk kareler.
        legend = tk.Frame(self.file_grid_frame, bg='white')
        legend.grid(row=0, column=0, columnspan=24, sticky='w', padx=4, pady=(2,7))
        def legend_item(parent, color, text):
            sw = tk.Label(parent, text='', bg=color, relief='solid', bd=1, width=2, height=1)
            sw.pack(side='left', padx=(0,4))
            tk.Label(parent, text=text, bg='white', fg='#526b7b', font=('Segoe UI',8)).pack(side='left', padx=(0,12))
        legend_item(legend, '#55b978', 'Tamam')
        legend_item(legend, '#f0b84f', 'Parca')
        legend_item(legend, '#d9e1e6', 'Kalan')

        cols = 24
        for i, (rel, size) in enumerate(self.file_grid_files):
            sq = tk.Label(self.file_grid_frame, text='', bg='#d9e1e6', relief='solid', bd=1,
                          width=2, height=1, cursor='hand2')
            r,c = divmod(i, cols)
            sq.grid(row=r+1, column=c, padx=2, pady=2)
            sq.bind('<Button-1>', lambda e, rr=rel: self._show_file_detail(rr))
            self.file_grid_items[rel] = sq
        self._refresh_file_grid(force=True)

    def _show_file_detail(self, rel: str):
        if not self.file_grid_dest:
            return
        target = self.file_grid_dest / clean_rel_path(rel)
        part = resolve_part_path(self.file_grid_dest, rel, migrate=False)
        text = f'Uzak göreli yol: {rel}\nYerel hedef: {target}\nParça: {part}'
        messagebox.showinfo('Dosya konumu', text)

    def _refresh_file_grid(self, force=False):
        try:
            if self.file_grid_dest and self.file_grid_files:
                done=partial=remaining=0; part_bytes=0; done_bytes=0
                for rel,size in self.file_grid_files:
                    lbl=self.file_grid_items.get(rel)
                    if not lbl: continue
                    target=self.file_grid_dest / clean_rel_path(rel)
                    part=resolve_part_path(self.file_grid_dest, rel, migrate=False)
                    status='remaining'; pct=0.0
                    try:
                        if target.exists() and (not size or target.stat().st_size == size):
                            status='done'; done+=1; done_bytes += int(size or target.stat().st_size)
                        elif part.exists():
                            ps=part.stat().st_size; part_bytes+=ps; partial+=1; status='partial'
                            pct=(ps/size*100.0) if size else 0.0
                        else:
                            remaining+=1
                    except OSError:
                        remaining+=1
                    if status=='done':
                        bg='#55b978'; tip='Tamamlandi'
                    elif status=='partial':
                        bg='#f0b84f'; tip=f'Parca %{pct:.0f}'
                    else:
                        bg='#d9e1e6'; tip='Kalan'
                    if force or lbl.cget('bg') != bg:
                        lbl.config(bg=bg)
                    # Isim gorunmez; Windows tooltip yerine tiklayinca ayrinti acilir.
                    try:
                        lbl.configure(takefocus=True)
                    except Exception:
                        pass
                total_bytes = self.dashboard_dataset_total_bytes or sum(int(sz or 0) for _,sz in self.file_grid_files)
                transferred = min(total_bytes, done_bytes + part_bytes) if total_bytes else done_bytes + part_bytes
                remain_bytes = max(0, total_bytes - transferred) if total_bytes else 0
                self.metric_vars['done'].set(str(done))
                self.metric_sub_vars['done'].set(f'{human_bytes(done_bytes)} tamam')
                self.metric_vars['remaining'].set(str(max(0, len(self.file_grid_files)-done)))
                self.metric_sub_vars['remaining'].set(f'{human_bytes(remain_bytes)} • {partial} yarım')
                self.metric_vars['total'].set(human_bytes(total_bytes))
                self.metric_sub_vars['total'].set(f'{len(self.file_grid_files)} dosya')
                self.file_grid_summary_var.set(f'Tamam {done} • Parça {partial} ({human_bytes(part_bytes)}) • Kalan {remaining} • Toplam {len(self.file_grid_files)}')
        except Exception:
            pass
        finally:
            if not force:
                self.after(1500, self._refresh_file_grid)

    def _set_last_job_banner(self, title: str, detail: str, state: str = 'idle', resumable: bool = False):
        palette = {
            'idle': ('#ffffff','#34566d','#6c7f8b'),
            'running': ('#e7f2f9','#155f8b','#155f8b'),
            'interrupted': ('#fff5df','#8a5b05','#8a5b05'),
            'complete': ('#eaf7ef','#176b3a','#176b3a'),
            'error': ('#fdeceb','#a43a32','#a43a32'),
        }
        bg, fg, dfg = palette.get(state, palette['idle'])
        self.last_job_title_var.set(title)
        self.last_job_detail_var.set(detail)
        self.last_job_title_lbl.config(bg=bg, fg=fg)
        self.last_job_detail_lbl.config(bg=bg, fg=dfg)
        try:
            self.last_job_title_lbl.master.config(bg=bg, highlightbackground=fg)
        except Exception:
            pass
        self.resume_now_btn.config(state='normal' if resumable else 'disabled')

    def _restore_last_job_status(self):
        st = load_last_job_state()
        self.last_job_state = st
        if not st:
            self._set_last_job_banner('Son iş: kayıt yok', 'Yeni bir aktarım başlatabilirsiniz.', 'idle', False)
            return
        dest = st.get('dest','')
        url = st.get('url','')
        if url and not self.url_var.get().strip(): self.url_var.set(url)
        if dest and (not self.dest_var.get().strip() or self.dest_var.get().strip().endswith('NGS_Download')): self.dest_var.set(dest)
        live = inspect_saved_live_status(dest) if dest else {}
        status = st.get('status','')
        stage = st.get('stage','')
        if status == 'completed':
            detail = st.get('summary') or f"{st.get('files_total','?')} dosya • final doğrulama temiz"
            self._set_last_job_banner('Son iş TAMAMLANDI', detail, 'complete', False)
            self.final_result_var.set('Durum: Son iş tamamlandı ve doğrulandı')
            return
        pct = float(live.get('percent',0) or 0)
        ft = int(live.get('files_total', st.get('files_total',0)) or 0)
        fc = int(live.get('files_completed',0) or 0)
        parts_detail = f'%{pct:.1f} • {fc}/{ft} dosya' if ft else (stage or 'yarım kaldı')
        label = 'Önceki iş YARIM KALDI' if status in ('running','interrupted','stopped') else 'Önceki iş TAMAMLANMADI'
        self._set_last_job_banner(label, parts_detail + ' • otomatik devam etmeye hazır', 'interrupted', bool(url and dest))
        self.final_result_var.set('Durum: Yarım iş bulundu • devam edebilir')
        if url and dest and not self.worker_thread:
            self.auto_resume_pending = True
            self._schedule_auto_resume()

    def _schedule_auto_resume(self):
        if not self.auto_resume_pending:
            return
        for sec in range(AUTO_RESUME_DELAY_SECONDS, 0, -1):
            self.after((AUTO_RESUME_DELAY_SECONDS-sec)*1000, lambda n=sec: self.last_job_detail_var.set(f'Yarım iş bulundu • {n} sn sonra otomatik devam edecek'))
        self.auto_resume_after_id = self.after(AUTO_RESUME_DELAY_SECONDS*1000, self._resume_saved_job_now)

    def _resume_saved_job_now(self):
        if self.worker_thread and self.worker_thread.is_alive():
            return
        st = load_last_job_state()
        if not st.get('url') or not st.get('dest'):
            return
        self.auto_resume_pending = False
        self.url_var.set(st['url']); self.dest_var.set(st['dest'])
        self._set_last_job_banner('Yarım iş sürdürülüyor', 'Sunucu ve yerel durum yeniden taranıyor...', 'running', False)
        self.auto_clicked()

    def _persist_job(self, status: str, stage: str, dest: Path, url: str, started: float, **extra):
        data = {
            'status': status, 'stage': stage, 'dest': str(dest), 'url': url,
            'started_epoch': started, 'started': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started)),
            'version': APP_VERSION,
        }
        data.update(extra)
        save_last_job_state(data)
        self.last_job_state = data
        try:
            if status == 'completed':
                self.post('job_banner', 'Son iş TAMAMLANDI', data.get('summary','Final doğrulama temiz'), 'complete', False)
            elif status == 'error':
                self.post('job_banner', 'İş TAMAMLANMADI', data.get('summary','Sorun bulundu'), 'error', True)
            elif status == 'interrupted':
                self.post('job_banner', 'İş YARIM KALDI', data.get('summary','Yarım dosyalar korundu'), 'interrupted', True)
            else:
                labels = {'starting':'Hazırlanıyor','scanned':'Dosyalar tarandı','downloading':'İndiriliyor','verifying':'Doğrulanıyor','repairing':'Hatalar onarılıyor'}
                self.post('job_banner', 'İş DEVAM EDİYOR', labels.get(stage, stage or 'Çalışıyor'), 'running', False)
        except Exception:
            pass

    def _prepare_dest(self) -> Optional[Path]:
        dest = Path(self.dest_var.get()).expanduser()
        try:
            safe_mkdir(dest)
            test = dest / '.ngs_write_test'; test.write_text('ok', encoding='utf-8'); test.unlink()
            set_report_location(True)
            rd = report_dir(dest)
            self.log_file_path = rd / 'NGS_DOWNLOAD.log'
            migrate_legacy_reports(dest, self.log)
            self._update_record_dir_label()
            return dest
        except Exception as e:
            messagebox.showerror(APP_NAME, f'Hedef klasöre yazılamıyor:\n{e}')
            return None

    def _validate_inputs(self) -> Optional[Path]:
        if self.worker_thread and self.worker_thread.is_alive(): return None
        if not self.url_var.get().strip():
            messagebox.showwarning(APP_NAME, 'Paylaşım bağlantısını girin.'); return None
        return self._prepare_dest()

    def auto_clicked(self):
        dest = self._validate_inputs()
        if not dest: return
        self.stop_event.clear(); self.pause_event.clear(); self._clear_verify_tree(); self._reset_stages()
        self.progress['value'] = 0; self.progress_var.set('Hazırlanıyor...')
        self.final_download_var.set('—'); self.final_verify_var.set('—'); self.final_time_var.set('—'); self.final_report_var.set('—'); self.final_result_var.set('Sonuç: —')
        self._reset_dashboard('Otomatik aktarım hazırlanıyor')
        self._set_connection_ui('checking','BAĞLANIYOR')
        self._set_busy('auto')
        self._save_current_state()
        url = self.url_var.get().strip(); pwd = self.password_var.get(); gzip_test = True; sound_done = bool(self.sound_done_var.get()); sound_error = bool(self.sound_error_var.get()); resume_parts = True; cleanup_success = bool(self.cleanup_success_var.get())
        self.worker_thread = threading.Thread(target=self._auto_worker, args=(dest, url, pwd, gzip_test, sound_done, sound_error, resume_parts, cleanup_success), daemon=True)
        self.worker_thread.start()

    def verify_only_clicked(self):
        dest = self._validate_inputs()
        if not dest: return
        self.stop_event.clear(); self.pause_event.clear(); self._clear_verify_tree(); self._reset_stages()
        self.progress['value'] = 0; self.progress_var.set('Hazırlanıyor...')
        self.final_download_var.set('—'); self.final_verify_var.set('—'); self.final_time_var.set('—'); self.final_report_var.set('—'); self.final_result_var.set('Sonuç: —')
        self._reset_dashboard('Bütünlük kontrolü hazırlanıyor')
        self._set_connection_ui('checking','BAĞLANIYOR')
        self._set_busy('verify')
        self._save_current_state()
        url = self.url_var.get().strip(); pwd = self.password_var.get(); gzip_test = True; sound_done = bool(self.sound_done_var.get()); sound_error = bool(self.sound_error_var.get()); cleanup_success = bool(self.cleanup_success_var.get())
        self.worker_thread = threading.Thread(target=self._verify_only_worker, args=(dest, url, pwd, gzip_test, sound_done, sound_error, cleanup_success), daemon=True)
        self.worker_thread.start()

    def _scan_internal(self, url: str, pwd: str):
        self.post('summary', 'Bağlantı tanınıyor...')
        self.post('connection','checking','BAĞLANIYOR')
        d = diagnose_share_url(url, pwd)
        diag = ' • '.join(x for x in [d.get('provider',''), (f"Nextcloud {d['version']}" if d.get('version') else ''), d.get('title','')] if x)
        self.log('Bağlantı: ' + (diag or d.get('kind','tanındı')))
        reconnects = 0
        alerted = False
        while not self.stop_event.is_set():
            self.post('summary', 'Sunucu ve dosyalar taranıyor...')
            try:
                cfg, files, total = scan_share(url, pwd, self.log, self.stop_event)
                if reconnects:
                    self.log(f'BAĞLANTI GERİ GELDİ: {reconnects} bekleme/yeniden deneme sonrası WebDAV yeniden erişilebilir.')
                    self.post('summary', 'Bağlantı geri geldi • dosya listesi alındı')
                    if alerted:
                        self.post('sound','recovered')
                self.initial_reconnects = reconnects
                self.cfg_obj, self.files, self.total_size = cfg, files, total
                if not files: raise RuntimeError('Paylaşımda indirilebilir dosya bulunamadı.')
                self.post('connection','connected','BAĞLI')
                self.log(f'Tarama: {len(files)} dosya • {human_bytes(total)}')
                return cfg, files, total
            except InterruptedError:
                raise
            except Exception as e:
                text = f'{type(e).__name__}: {e}'
                low = text.lower()
                permanent = any(x in low for x in ('http 401','http 403','http 404','parola','password')) and not any(x in low for x in ('connectionerror','readtimeout','connecttimeout','timed out','timeout','http 502','http 503','http 504','http 429'))
                transient = any(x in low for x in ('connectionerror','readtimeout','connecttimeout','timed out','timeout','http 429','http 500','http 502','http 503','http 504','temporarily unavailable','name or service not known','getaddrinfo'))
                if permanent or not transient:
                    self.post('connection','error','HATA')
                    raise
                reconnects += 1
                self.post('connection','waiting','BEKLENİYOR')
                wait_s = min(120, INITIAL_RECONNECT_SECONDS * (1 if reconnects <= 2 else 2 if reconnects <= 4 else 4))
                self.log(f'AĞ/SUNUCU GEÇİCİ OLARAK YOK: {text} • {wait_s} sn sonra otomatik yeniden denenecek.')
                if not alerted:
                    alerted = True
                    if self.sound_error_var.get(): self.post('sound','server')
                end = time.time() + wait_s
                while time.time() < end and not self.stop_event.is_set():
                    rem = max(0, int(end-time.time()))
                    self.post('summary', f'Ağ/sunucu bekleniyor • yeniden denemeye {rem:02d} sn')
                    time.sleep(1)
        raise InterruptedError('Tarama kullanıcı tarafından durduruldu.')

    @staticmethod
    def _auto_workers(test_result: dict, file_count: int, stream_result: Optional[dict] = None) -> int:
        stable = max(1, int(test_result.get('stable_max', 1)))
        rows = test_result.get('results') or []
        one = next((r for r in rows if int(r.get('level',0)) == 1), None)
        latency = float((one or {}).get('median_latency', 0.0) or 0.0)

        # Metadata latency is an early warning. A server taking >1.5 s even for
        # Depth:0 PROPFIND should not immediately receive four multi-GB streams.
        if latency >= 1.5:
            chosen = 1
        elif latency >= 0.60:
            chosen = 2 if stable >= 2 else 1
        elif stable >= 4:
            chosen = 4
        elif stable >= 2:
            chosen = 2
        else:
            chosen = 1

        # A tiny real payload probe can further reduce the initial concurrency.
        if stream_result:
            if stream_result.get('ok') is False:
                chosen = 1
            elif stream_result.get('ok') is True:
                sp = float(stream_result.get('speed_bps',0.0) or 0.0)
                if sp < 1 * 1024 * 1024:
                    chosen = 1
                elif sp < 4 * 1024 * 1024:
                    chosen = min(chosen, 2)

        return max(1, min(chosen, max(1, file_count), 4))

    def _check_disk(self, dest: Path, missing: int):
        free = shutil.disk_usage(dest).free
        reserve = max(2 * 1024**3, int(missing * 0.02)) if missing > 0 else 0
        if free < missing + reserve:
            raise RuntimeError(f'Hedef diskte yeterli güvenli boş alan yok. Kalan {human_bytes(missing)}, boş {human_bytes(free)}, güvenlik payı {human_bytes(reserve)}.')
        self.log(f'Disk: indirilecek ~{human_bytes(missing)}, boş {human_bytes(free)}')

    def _run_verification(self, dest: Path, gzip_test: bool):
        self.post('mode', 'verify'); self.post('clear_verify')
        mgr = VerificationManager(self.files, dest, self.log,
            lambda fd,ft,bd,bt,el: self.post('verify_progress', fd,ft,bd,bt,el),
            lambda st: self.post('summary', st),
            lambda *a: self.post('verify_item', *a),
            self.stop_event, gzip_test=gzip_test)
        return mgr.run()

    @staticmethod
    def _verification_problem_count(result: Optional[dict]) -> int:
        if not result:
            return 0
        return (result.get('partial', 0) + result.get('missing', 0) +
                result.get('mismatch', 0) + result.get('error', 0))

    def _file_by_rel(self) -> Dict[str, RemoteFile]:
        return {normalize_rel_for_match(rf.rel_path): rf for rf in self.files}

    def _repair_selection(self, verify_result: dict) -> Tuple[List[RemoteFile], List[dict]]:
        """Return the smallest useful set of remote files to fetch again.

        Data-integrity failures redownload the data file from byte zero. A vendor
        checksum sidecar parsing failure redownloads the sidecar first; the data
        file itself is left intact unless its checksum/CRC actually failed.
        """
        by_key = self._file_by_rel()
        selected: Dict[str, RemoteFile] = {}
        rows = list(verify_result.get('problem_rows') or [])
        for row in rows:
            rel = row.get('relative_path', '')
            if not rel:
                continue
            key = normalize_rel_for_match(rel)
            status = row.get('status', '')
            if status == 'VENDOR_CHECKSUM_UNREADABLE':
                found_sidecar = False
                for suffix in CHECKSUM_SIDECAR_SUFFIXES:
                    side = by_key.get(key + suffix)
                    if side is not None:
                        selected[normalize_rel_for_match(side.rel_path)] = side
                        found_sidecar = True
                if found_sidecar:
                    continue
            rf = by_key.get(key)
            if rf is not None:
                selected[key] = rf
        return list(selected.values()), rows

    def _remove_for_clean_redownload(self, dest: Path, verify_rows: List[dict]):
        """Remove only files proven bad; partial/missing files keep resume state."""
        by_key = self._file_by_rel()
        destructive = {
            'SIZE_MISMATCH', 'CHECKSUM_MISMATCH', 'CHECKSUM_AND_GZIP_ERROR',
            'GZIP_ERROR', 'VERIFY_ERROR'
        }
        for row in verify_rows:
            rel = row.get('relative_path', '')
            status = row.get('status', '')
            if not rel:
                continue
            key = normalize_rel_for_match(rel)
            if status == 'VENDOR_CHECKSUM_UNREADABLE':
                # Redownload the small checksum sidecar(s), not a healthy multi-GB FASTQ.
                for suffix in CHECKSUM_SIDECAR_SUFFIXES:
                    side = by_key.get(key + suffix)
                    if side is None:
                        continue
                    target = dest / clean_rel_path(side.rel_path)
                    part = resolve_part_path(dest, side.rel_path, migrate=False)
                    for path in (target, part):
                        try:
                            if path.exists():
                                path.unlink()
                                self.log(f'Otomatik onarım: checksum yan dosyası silindi, yeniden alınacak: {side.rel_path}')
                        except OSError as e:
                            raise RuntimeError(f'Onarım için checksum dosyası silinemedi: {path} | {e}')
                continue
            if status not in destructive:
                # PARTIAL is intentionally preserved so Range resume can finish it.
                continue
            rf = by_key.get(key)
            if rf is None:
                continue
            target = dest / clean_rel_path(rf.rel_path)
            part = resolve_part_path(dest, rf.rel_path, migrate=False)
            for path in (target, part):
                try:
                    if path.exists():
                        sz = path.stat().st_size
                        path.unlink()
                        self.log(f'Otomatik onarım: hatalı dosya/parça silindi ({human_bytes(sz)}), baştan indirilecek: {rf.rel_path}')
                except OSError as e:
                    raise RuntimeError(f'Onarım için hatalı dosya silinemedi: {path} | {e}')

    def _run_repair_download(self, cfg: EndpointConfig, subset: List[RemoteFile], dest: Path,
                             workers: int, label: str) -> dict:
        if not subset:
            return {'ok': 0, 'skipped': 0, 'failed': 0, 'failed_paths': []}
        workers = max(1, min(int(workers or 1), 2, len(subset)))
        self.post('mode', 'download')
        self.post('summary', f'{label} • {len(subset)} dosya • {workers} güvenli bağlantı')
        self.log(f'{label}: {len(subset)} dosya yeniden indirilecek/devam edecek; paralellik {workers}.')
        mgr = DownloadManager(cfg, subset, dest, workers, self.log,
            lambda d,t,fd,ft,sp,eta: self.post('progress', d,t,fd,ft,sp,eta),
            lambda st: self.post('summary', st), self.stop_event, self.pause_event,
            lambda kind: self.post('sound', kind))
        return mgr.run()

    def _repair_initial_download_failures(self, cfg: EndpointConfig, dest: Path,
                                          download_result: dict) -> Tuple[dict, int, int]:
        """Do not start final verification while known download failures remain."""
        by_key = self._file_by_rel()
        pending = [by_key[k] for k in [normalize_rel_for_match(x) for x in download_result.get('failed_paths', [])] if k in by_key]
        rounds = 0
        repaired_ok = 0
        initial_failed = len(pending)
        if pending:
            self.post('repair_queue', [rf.rel_path for rf in pending], 0, AUTO_REPAIR_MAX_ROUNDS, repaired_ok, 'İlk indirme hataları')
        while pending and not self.stop_event.is_set() and rounds < AUTO_REPAIR_MAX_ROUNDS:
            rounds += 1
            self.log(f'İlk aktarım hataları için otomatik onarım turu {rounds}/{AUTO_REPAIR_MAX_ROUNDS}: {len(pending)} dosya.')
            self.post('stage','repair','active',f'İndirme onarımı • tur {rounds}/{AUTO_REPAIR_MAX_ROUNDS} • {len(pending)} dosya'); self.post('summary', f'İndirme hataları otomatik düzeltiliyor • tur {rounds}/{AUTO_REPAIR_MAX_ROUNDS} • {len(pending)} dosya')
            self.post('repair_queue', [rf.rel_path for rf in pending], rounds, AUTO_REPAIR_MAX_ROUNDS, repaired_ok, 'İndirme hatası • otomatik yeniden indirme')
            if rounds > 1:
                end = time.time() + AUTO_REPAIR_PAUSE_SECONDS
                while time.time() < end and not self.stop_event.is_set():
                    time.sleep(0.2)
            rr = self._run_repair_download(cfg, pending, dest, 1, f'İndirme hata onarımı {rounds}')
            repaired_ok += rr.get('ok', 0)
            failed_keys = {normalize_rel_for_match(x) for x in rr.get('failed_paths', [])}
            pending = [by_key[k] for k in failed_keys if k in by_key]
            self.post('repair_queue', [rf.rel_path for rf in pending], rounds, AUTO_REPAIR_MAX_ROUNDS, repaired_ok, 'İndirme hatası • otomatik yeniden indirme')
        download_result['initial_failed'] = initial_failed
        download_result['repair_rounds_download'] = rounds
        download_result['repair_downloaded'] = repaired_ok
        download_result['failed'] = len(pending)
        download_result['failed_paths'] = [rf.rel_path for rf in pending]
        return download_result, rounds, repaired_ok

    def _repair_after_verification(self, cfg: EndpointConfig, dest: Path, gzip_test: bool,
                                   verify_result: dict, download_result: dict) -> Tuple[dict, dict]:
        """Self-healing final audit: bad files are fetched again and the full audit repeats.

        A TAM OTOMATIK session is never called successful while any verification
        problem remains. Persistent external/server faults are reported as
        incomplete rather than being silently accepted.
        """
        rounds = 0
        repair_attempted = 0
        repaired_downloads = 0
        while (not self.stop_event.is_set() and
               self._verification_problem_count(verify_result) > 0 and
               rounds < AUTO_REPAIR_MAX_ROUNDS):
            rounds += 1
            subset, rows = self._repair_selection(verify_result)
            if not subset:
                self.log('Otomatik onarım: sorun var ancak yeniden indirilecek uzak dosya eşleştirilemedi.')
                break
            repair_attempted += len(subset)
            self.post('stage','repair','active',f'Bütünlük onarımı • tur {rounds}/{AUTO_REPAIR_MAX_ROUNDS} • {len(subset)} dosya'); self.post('summary', f'Final doğrulama hataları onarılıyor • tur {rounds}/{AUTO_REPAIR_MAX_ROUNDS} • {len(subset)} dosya')
            self.post('repair_queue', [rf.rel_path for rf in subset], rounds, AUTO_REPAIR_MAX_ROUNDS, repaired_downloads, 'Doğrulama hatası • baştan/yeniden indirme')
            self.log(f'Final doğrulama otomatik onarım turu {rounds}: {len(subset)} dosya.')
            self._remove_for_clean_redownload(dest, rows)
            rr = self._run_repair_download(cfg, subset, dest, 1, f'Bütünlük onarımı {rounds}')
            repaired_downloads += rr.get('ok', 0)
            self.post('repair_queue', list(rr.get('failed_paths', []) or []), rounds, AUTO_REPAIR_MAX_ROUNDS, repaired_downloads, 'Yeniden indirme sonrası doğrulama bekleniyor')
            if self.stop_event.is_set():
                break
            self.post('stage','repair','ok',f'Onarım turu {rounds} tamam'); self.post('stage','verify','active','Tüm veri seti yeniden doğrulanıyor'); self.post('summary', f'Onarım turu {rounds} bitti • tüm veri seti yeniden doğrulanıyor...')
            verify_result = self._run_verification(dest, gzip_test)
        download_result['repair_rounds_verify'] = rounds
        download_result['repair_attempted_files'] = repair_attempted
        download_result['repair_downloaded'] = download_result.get('repair_downloaded', 0) + repaired_downloads
        download_result['final_verify_problems'] = self._verification_problem_count(verify_result)
        if download_result['final_verify_problems'] == 0:
            download_result['failed'] = 0
            download_result['failed_paths'] = []
            self.post('repair_queue', [], rounds, AUTO_REPAIR_MAX_ROUNDS, repaired_downloads, 'Onarım tamamlandı')
        return verify_result, download_result

    def _write_final_status_json(self, dest: Path, verify_result: Optional[dict], download_result: Optional[dict] = None):
        """Persist the final verification state; download completion alone is not final success."""
        try:
            status_path = report_dir(dest) / 'NGS_DOWNLOAD_STATUS.json'
            try:
                data = json.loads(status_path.read_text(encoding='utf-8')) if status_path.exists() else {}
            except Exception:
                data = {}
            vr = verify_result or {}
            problems = self._verification_problem_count(vr)
            full_verified = bool(vr.get('full_verified', False))
            data.update({
                'app': APP_NAME,
                'version': APP_VERSION,
                'updated': time.strftime('%Y-%m-%d %H:%M:%S'),
                'phase': 'verification_complete',
                'status': 'verified' if full_verified else ('verification_failed' if problems else 'verification_warning'),
                'verification_problems': problems,
                'full_verified': full_verified,
                'fastq_total': vr.get('fastq_total', 0),
                'checksum_sidecars_found': vr.get('vendor_sidecar_expected', 0),
                'checksum_sidecars_parsed': vr.get('fastq_vendor_expected', 0),
                'source_checksum_ok': vr.get('fastq_vendor_ok', 0),
                'gzip_crc_ok': vr.get('fastq_crc_ok', 0),
                'verification_report': vr.get('report', ''),
            })
            if download_result is not None:
                data['files_failed'] = download_result.get('failed', data.get('files_failed', 0))
            tmp = status_path.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            os.replace(tmp, status_path)
        except Exception as e:
            self.log(f'Final durum JSON yazılamadı: {e}')

    def _write_session_summary(self, dest: Path, mode: str, started: float, ended: float,
                               download_result: Optional[dict], verify_result: Optional[dict],
                               workers: Optional[int], stable_max: Optional[int], missing_before: int = 0, source_url: str = '') -> Path:
        rd = report_dir(dest)
        problems = 0
        if verify_result:
            problems = verify_result.get('partial',0)+verify_result.get('missing',0)+verify_result.get('mismatch',0)+verify_result.get('error',0)
        elapsed = max(0, ended-started)
        dl_line = 'İndirme yapılmadı (sadece doğrulama modu).'
        if download_result is not None:
            avg = (missing_before / max(download_result.get('elapsed', elapsed), 0.1)) if missing_before else 0
            spn = download_result.get('server_protection_activations',0)
            spw = download_result.get('server_wait_seconds',0)
            repair_rounds = download_result.get('repair_rounds_download',0) + download_result.get('repair_rounds_verify',0)
            repair_files = download_result.get('repair_downloaded',0)
            initial_failed = download_result.get('initial_failed', download_result.get('failed',0))
            dl_line = (f"İlk aktarım yeni: {download_result.get('ok',0)} | Zaten sağlam: {download_result.get('skipped',0)} | "
                       f"İlk aktarım hatası: {initial_failed} | Otomatik onarım: {repair_rounds} tur / {repair_files} başarılı yeniden indirme | "
                       f"FINAL indirme hatası: {download_result.get('failed',0)} | Aktarılması gereken: ~{human_bytes(missing_before)} | "
                       f"Yaklaşık ortalama hız: {human_bytes(avg)}/sn | "
                       f"Sunucu koruma: {spn} kez / bekleme {format_duration(spw)}")
        vr_line = 'Doğrulama yapılmadı.'
        if verify_result:
            fastq_total = verify_result.get('fastq_total',0)
            fastq_vendor_ok = verify_result.get('fastq_vendor_ok',0)
            fastq_crc_ok = verify_result.get('fastq_crc_ok',0)
            sidecar_found = verify_result.get('vendor_sidecar_expected',0)
            sidecar_parsed = verify_result.get('fastq_vendor_expected',0)
            vr_line = (f"Dosya sorunu: {problems} | Checksum sidecar bulunan: {sidecar_found}/{fastq_total} | "
                       f"Sidecar eşleştirilen: {sidecar_parsed}/{fastq_total} | Kaynak MD5/SHA eşleşen: {fastq_vendor_ok}/{fastq_total} | "
                       f"GZIP/CRC FASTQ: {fastq_crc_ok}/{fastq_total} (otomatik) | "
                       f"Kaynak hash doğrulanan dosya: {verify_result.get('verified_hash',0)} | "
                       f"Yarım: {verify_result.get('partial',0)} | Eksik: {verify_result.get('missing',0)} | "
                       f"Uyuşmazlık: {verify_result.get('mismatch',0)} | Okuma/parsing hatası: {verify_result.get('error',0)}")
        lines = [
            'NGS SAFE DOWNLOADER - OTURUM ÖZETİ',
            '='*48,
            f'Mod: {mode}',
            f'Başlangıç: {time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started))}',
            f'Bitiş: {time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ended))}',
            f'Toplam süre: {format_duration(elapsed)}',
            f'Kaynak: {source_url}',
            f'Hedef: {dest}',
            f'Parça klasörü: {part_dir(dest)}',
            f'Kesinti davranışı: yarım dosyadan otomatik devam',
            f'Dosya: {len(self.files)} | Toplam: {human_bytes(self.total_size)}',
            (f'Paralellik: başlangıç {download_result.get("parallel_initial", workers) if download_result else (workers if workers else "-")} | '
             f'en düşük {download_result.get("parallel_min", "-") if download_result else "-"} | '
             f'final {download_result.get("parallel_final", "-") if download_result else "-"} | stabil metadata test {stable_max if stable_max else "-"}'),
            '', 'İNDİRME', dl_line, '', 'DOĞRULAMA', vr_line, '',
            f'Doğrulama raporu: {verify_result.get("report", "-") if verify_result else "-"}',
            f'Detay CSV: {verify_result.get("csv_report", "-") if verify_result else "-"}',
        ]
        text = '\n'.join(lines) + '\n'
        latest = rd / 'SON_DURUM.txt'
        latest.write_text(text, encoding='utf-8')
        stamp = time.strftime('%Y%m%d_%H%M%S', time.localtime(ended))
        history = rd / f'oturum_{stamp}.txt'
        history.write_text(text, encoding='utf-8')
        return latest

    def _post_final(self, dest: Path, started: float, ended: float, download_result, verify_result, workers, report_path):
        if download_result is None:
            dl = 'Sadece doğrulama; indirme yapılmadı.'
        else:
            p0 = download_result.get('parallel_initial', workers); pf = download_result.get('parallel_final', workers); pm = download_result.get('parallel_min', pf)
            spn = download_result.get('server_protection_activations',0); spw = download_result.get('server_wait_seconds',0)
            server_txt = f" • sunucu bekleme {spn}x/{format_duration(spw)}" if spn else ""
            rr = download_result.get('repair_rounds_download',0) + download_result.get('repair_rounds_verify',0)
            rd = download_result.get('repair_downloaded',0)
            repair_txt = f" • otomatik onarım {rr} tur/{rd} dosya" if rr else ""
            dl = f"Yeni {download_result.get('ok',0)} • zaten vardı {download_result.get('skipped',0)} • FINAL hata {download_result.get('failed',0)}{repair_txt} • paralel {p0}→{pf} (min {pm}){server_txt}"
        problems = verify_result.get('partial',0)+verify_result.get('missing',0)+verify_result.get('mismatch',0)+verify_result.get('error',0)
        fq = verify_result.get('fastq_total',0)
        md5ok = verify_result.get('fastq_vendor_ok',0)
        crcok = verify_result.get('fastq_crc_ok',0)
        sidecar_found = verify_result.get('vendor_sidecar_expected',0)
        sidecar_parsed = verify_result.get('fastq_vendor_expected',0)
        deep = True
        full_verified = bool(verify_result.get('full_verified', False))
        if full_verified:
            vr = f"FASTQ {fq}/{fq} • sidecar {sidecar_parsed}/{sidecar_found} • MD5/SHA {md5ok}/{fq} • CRC {crcok}/{fq} • TAM DOĞRULANDI"
        else:
            vr = f"sidecar {sidecar_parsed}/{sidecar_found} • MD5/SHA {md5ok}/{fq} • CRC {crcok}/{fq} • sorun {problems} • KONTROL GEREKİYOR"
        tm = (f"Başlangıç {time.strftime('%H:%M:%S', time.localtime(started))} • "
              f"Bitiş {time.strftime('%H:%M:%S', time.localtime(ended))} • Süre {format_duration(ended-started)}")
        self.post('final', dl, vr, tm, str(report_path.parent))

    def _auto_worker(self, dest: Path, url: str, pwd: str, gzip_test: bool, sound_done: bool, sound_error: bool, resume_parts: bool = True, cleanup_success: bool = False):
        started = time.time(); download_result = None; verify_result = None; workers = 1; stable_max = 1; missing_before = 0
        self._persist_job('running','starting',dest,url,started)
        try:
            self.post('stage','download','active','Bağlantı ve dosya listesi hazırlanıyor')
            self.post('stage','verify','idle','İndirme sonrasında başlayacak')
            self.post('stage','repair','idle','Gerekirse otomatik çalışacak')
            self.log(f'TAM OTOMATİK başladı: {time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started))}')
            cfg, files, total = self._scan_internal(url, pwd)
            self._persist_job('running','scanned',dest,url,started,files_total=len(files),total_bytes=total)
            migrate_legacy_parts(files, dest, self.log)
            self.post('file_grid_init', [(rf.rel_path, rf.size) for rf in files], str(dest))
            if self.stop_event.is_set(): return
            if not resume_parts:
                n_parts, b_parts = clear_partial_files(files, dest, self.log)
                if n_parts:
                    self.post('summary', f'Kesinti sonrası baştan indirme seçili • {n_parts} yarım parça temizlendi')
            self.post('summary', 'Güvenli paralellik otomatik ölçülüyor...')
            test = test_parallel_connections(cfg, self.log, self.stop_event, max_workers=8)
            self.post('summary', 'Kısa gerçek veri akışı testi yapılıyor...')
            stream_test = test_real_stream(cfg, files, self.log, self.stop_event)
            stable_max = int(test.get('stable_max',1)); workers = self._auto_workers(test, len(files), stream_test)
            if self.initial_reconnects:
                self.post('summary', 'Bağlantı geri geldi • gerçek veri ile 1→2→3 nazik paralellik kontrolü...')
                rr = test_recovery_payload_parallel(cfg, files, self.log, self.stop_event)
                real_stable = int(rr.get('stable_max',1) or 1)
                if real_stable <= 1:
                    workers = 1
                elif real_stable >= 2:
                    # Recovery after an outage is intentionally conservative: even if 3
                    # small streams work, start with at most 2 and let adaptive control grow later.
                    workers = max(workers, 2)
                    workers = min(workers, 2)
                self.log(f'Bağlantı dönüş gerçek payload sonucu: stabil {real_stable}; başlangıç {workers} bağlantı seçildi.')
            self.post('parallel', stable_max, workers, bool(test.get('ceiling_reached')))
            self.log(f'Başlangıç paralelliği otomatik seçildi: {workers} (metadata stabil {stable_max}; payload test {human_bytes(stream_test.get("speed_bps",0))}/sn)')
            missing_before = calculate_missing_bytes(files, dest)
            self._check_disk(dest, missing_before)
            if self.stop_event.is_set(): return
            self._persist_job('running','downloading',dest,url,started,files_total=len(files),total_bytes=total)
            self.post('mode','download'); self.post('stage','download','active',f'İndiriliyor • {workers} bağlantı'); self.post('summary', f'İndirme/devam başladı • {workers} paralel bağlantı')
            mgr = DownloadManager(cfg, files, dest, workers, self.log,
                lambda d,t,fd,ft,sp,eta: self.post('progress', d,t,fd,ft,sp,eta),
                lambda s: self.post('summary', s), self.stop_event, self.pause_event,
                lambda kind: self.post('sound', kind))
            download_result = mgr.run()
            if self.stop_event.is_set():
                self._persist_job('interrupted','download',dest,url,started,files_total=len(files),summary='Güvenli durduruldu; yarım dosyalar korundu')
                self.post('summary','Güvenli durduruldu • .part dosyaları korundu')
                return

            # v1.9: A TAM OTOMATİK run must not proceed as if successful while the
            # downloader already knows that some files failed. Retry only those
            # files first, conservatively, preserving usable .part data.
            if download_result.get('failed_paths'):
                self.post('stage','download','warn',f"{len(download_result.get('failed_paths', []))} indirme hatası bulundu"); self.post('stage','repair','active','İndirme hataları yeniden alınıyor'); self.post('summary', f"İlk indirmede {len(download_result.get('failed_paths', []))} hata var • otomatik onarım başlıyor...")
                download_result, _, _ = self._repair_initial_download_failures(cfg, dest, download_result)
                if self.stop_event.is_set():
                    self.post('summary','Güvenli durduruldu • .part dosyaları korundu')
                    return

            self._persist_job('running','verifying',dest,url,started,files_total=len(files))
            self.post('stage','download','ok',f'{len(files)}/{len(files)} dosya aktarıldı'); self.post('stage','verify','active','Checksum / CRC doğrulaması başlıyor'); self.post('summary', 'İndirme aşaması bitti • final bütünlük doğrulaması başlıyor...')
            verify_result = self._run_verification(dest, gzip_test)

            # v1.9 self-healing audit. Missing/partial files resume; size/hash/CRC
            # failures are deleted and fetched from byte zero, then the complete
            # data set is audited again. Never silently accept a non-zero result.
            if self._verification_problem_count(verify_result) > 0:
                self._persist_job('running','repairing',dest,url,started,files_total=len(files),problems=self._verification_problem_count(verify_result))
                self.post('stage','verify','warn',f"{self._verification_problem_count(verify_result)} bütünlük sorunu bulundu"); self.post('stage','repair','active','Hatalı dosyalar yeniden indiriliyor'); self.post('summary', f"Doğrulamada {self._verification_problem_count(verify_result)} sorun bulundu • otomatik yeniden indirme başlıyor...")
                verify_result, download_result = self._repair_after_verification(
                    cfg, dest, gzip_test, verify_result, download_result
                )
                if self.stop_event.is_set():
                    self.post('summary','Güvenli durduruldu • onarım durumu ve .part dosyaları korundu')
                    return

            ended = time.time()
            report_path = self._write_session_summary(dest, 'TAM OTOMATİK', started, ended, download_result, verify_result, workers, stable_max, missing_before, url)
            problems = verify_result.get('partial',0)+verify_result.get('missing',0)+verify_result.get('mismatch',0)+verify_result.get('error',0)+download_result.get('failed',0)
            full_verified = bool(verify_result.get('full_verified', False)) and download_result.get('failed',0) == 0
            self._write_final_status_json(dest, verify_result, download_result)
            if problems or not full_verified:
                summary = (f'{problems} sorun kaldı' if problems else 'Kaynak checksum / CRC doğrulaması tam değil')
                self._persist_job('error','incomplete',dest,url,started,files_total=len(files),problems=problems,summary=summary)
                self.post('stage','verify','error',f'{problems} sorun kaldı' if problems else 'Tam doğrulama tamamlanmadı')
                self.post('stage','repair','error','Otomatik onarım tamamlayamadı' if problems else 'Yeniden indirme gerekmiyor • kaynak doğrulaması eksik')
                self.post('summary', f'TAMAMLANMADI • {summary}')
                if sound_error and not self.verify_problem_beeped: self.post('sound','error')
            else:
                self._persist_job('completed','complete',dest,url,started,files_total=len(files),problems=0,summary=f'{len(files)}/{len(files)} dosya sağlam • kaynak MD5/SHA + FASTQ.GZ CRC doğrulaması geçti')
                self.post('stage','download','ok',f'{len(files)}/{len(files)} dosya tamam')
                self.post('stage','verify','ok','Kaynak MD5/SHA + GZIP/CRC temiz')
                repair_rounds = (download_result.get('repair_rounds_download',0) + download_result.get('repair_rounds_verify',0)) if download_result else 0
                self.post('stage','repair','ok',f'{repair_rounds} onarım turu' if repair_rounds else 'Gerekmedi')
                self.post('summary', f'TAM DOĞRULANDI • {len(files)}/{len(files)} dosya sağlam')
                if sound_done: self.post('sound','done')
            self.log(f'Oturum bitti: {time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ended))} • süre {format_duration(ended-started)}')
            archive = self._archive_and_cleanup_verification_records(dest, ended, full_verified)
            if archive:
                if verify_result.get('report'): verify_result['report'] = str(archive / Path(verify_result['report']).name)
                if verify_result.get('csv_report'): verify_result['csv_report'] = str(archive / Path(verify_result['csv_report']).name)
                report_path = archive / Path(report_path).name
            self._post_final(dest, started, ended, download_result, verify_result, workers, report_path)
            if full_verified:
                prune_empty_part_tree(dest)
        except Exception as e:
            self._persist_job('error','exception',dest,url,started,summary=str(e))
            self.post('stage','download','error','İşlem kesildi')
            self.log(traceback.format_exc()); self.post('error', str(e))
        finally:
            self.post('idle')

    def _verify_only_worker(self, dest: Path, url: str, pwd: str, gzip_test: bool, sound_done: bool, sound_error: bool, cleanup_success: bool = False):
        started = time.time(); verify_result = None
        try:
            self.post('stage','download','idle','Atlandı • sadece doğrulama')
            self.post('stage','verify','active','Dosya listesi hazırlanıyor')
            self.post('stage','repair','idle','Bu modda onarım yok')
            self.log(f'SADECE DOĞRULA başladı: {time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started))}')
            self._scan_internal(url, pwd)
            migrate_legacy_parts(self.files, dest, self.log)
            self.post('file_grid_init', [(rf.rel_path, rf.size) for rf in self.files], str(dest))
            if self.stop_event.is_set(): return
            verify_result = self._run_verification(dest, gzip_test)
            ended = time.time()
            report_path = self._write_session_summary(dest, 'SADECE DOĞRULA', started, ended, None, verify_result, None, None, 0, url)
            problems = verify_result.get('partial',0)+verify_result.get('missing',0)+verify_result.get('mismatch',0)+verify_result.get('error',0)
            full_verified = bool(verify_result.get('full_verified', False))
            self._write_final_status_json(dest, verify_result, None)
            if problems or not full_verified:
                self.post('stage','verify','error',f'{problems} sorun bulundu' if problems else 'Tam doğrulama tamamlanmadı')
                self.post('summary', f'Doğrulama tamamlandı • {problems} sorun bulundu' if problems else 'Doğrulama tamamlandı • kaynak checksum/CRC tam değil')
                if sound_error and not self.verify_problem_beeped: self.post('sound','error')
            else:
                self.post('stage','verify','ok',f'{len(self.files)}/{len(self.files)} dosya • MD5/SHA + CRC temiz')
                self.post('summary', f'TAM DOĞRULANDI • {len(self.files)}/{len(self.files)} dosya sağlam')
                if sound_done: self.post('sound','done')
            self.log(f'Doğrulama bitti: {time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ended))} • süre {format_duration(ended-started)}')
            archive = self._archive_and_cleanup_verification_records(dest, ended, full_verified)
            if archive:
                if verify_result.get('report'): verify_result['report'] = str(archive / Path(verify_result['report']).name)
                if verify_result.get('csv_report'): verify_result['csv_report'] = str(archive / Path(verify_result['csv_report']).name)
                report_path = archive / Path(report_path).name
            self._post_final(dest, started, ended, None, verify_result, None, report_path)
            if full_verified:
                prune_empty_part_tree(dest)
        except Exception as e:
            self.post('stage','verify','error','Doğrulama kesildi')
            self.log(traceback.format_exc()); self.post('error', str(e))
        finally:
            self.post('idle')

    def pause_clicked(self):
        if self.active_mode != 'download': return
        if self.pause_event.is_set():
            self.pause_event.clear(); self.pause_btn.config(text='Duraklat'); self.log('İndirme devam ettirildi.')
        else:
            self.pause_event.set(); self.pause_btn.config(text='Devam et'); self.log('İndirme duraklatıldı.')

    def stop_clicked(self):
        self.pause_event.clear(); self.stop_event.set(); self.stop_btn.config(state='disabled'); self.pause_btn.config(state='disabled', text='Duraklat')
        self.log('Güvenli durdurma istendi. .part dosyaları korunacak.')

    def on_close(self):
        if self.worker_thread and self.worker_thread.is_alive():
            if not messagebox.askyesno(APP_NAME, 'Aktif işlem var. Güvenli biçimde durdurup kapatmak istiyor musunuz?'): return
            self.pause_event.clear(); self.stop_event.set()
        self.destroy()


if __name__ == '__main__':
    app = App()
    app.protocol('WM_DELETE_WINDOW', app.on_close)
    app.mainloop()
