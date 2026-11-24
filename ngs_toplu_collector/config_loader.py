from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

_PLACEHOLDER_MARKERS=("your_analysis_platform","example.invalid")

def _url_is_real(value: str) -> bool:
    value=str(value or "").strip()
    if not value:return False
    low=value.casefold()
    if any(x in low for x in _PLACEHOLDER_MARKERS):return False
    u=urlparse(value)
    return bool(u.scheme in ("http","https") and u.netloc)

def platform_origin(cfg: dict) -> str:
    """Return the real analysis origin without ever trusting public placeholders.

    A local install may only override runs_url. In that common case the public
    config.example base_url must NOT win over the authenticated Runs origin.
    """
    # The Runs URL is authoritative for UI-detail links because it is the
    # address the persistent authenticated browser actually opened. This also
    # protects against an old/stale base_url left in a local config.
    runs=str(cfg.get("runs_url") or "").strip()
    if _url_is_real(runs):
        u=urlparse(runs);return f"{u.scheme}://{u.netloc}"
    base=str(cfg.get("base_url") or "").strip().rstrip("/")
    if _url_is_real(base):
        u=urlparse(base);return f"{u.scheme}://{u.netloc}"
    return ""

def load_config(base: Path) -> dict:
    """Load public defaults, then optional private local overrides.

    Priority (low -> high):
      config.example.json -> legacy config.json -> config.local.json -> env vars.

    `config.local.json` and legacy `config.json` are intentionally ignored by git.
    """
    cfg: dict = {}
    for name in ("config.example.json", "config.json", "config.local.json"):
        p = base / name
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):cfg.update(data)
    if os.getenv("NGS_PLATFORM_RUNS_URL"):cfg["runs_url"] = os.environ["NGS_PLATFORM_RUNS_URL"].strip()
    if os.getenv("NGS_PLATFORM_BASE_URL"):cfg["base_url"] = os.environ["NGS_PLATFORM_BASE_URL"].strip().rstrip("/")
    cfg.setdefault("base_url", "");cfg.setdefault("runs_url", "")
    # Critical: example base_url is non-empty, so "if not base_url" is not enough.
    # Replace any placeholder with the authenticated Runs origin.
    origin=platform_origin(cfg)
    if origin:cfg["base_url"]=origin
    elif not _url_is_real(cfg.get("base_url")):cfg["base_url"]=""
    version_file=base/'VERSION'
    if version_file.exists():cfg['ui_version']=version_file.read_text(encoding='utf-8').strip()
    return cfg

def config_ready(cfg: dict) -> bool:
    return _url_is_real(str(cfg.get("runs_url") or ""))
