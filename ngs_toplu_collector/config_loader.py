from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse


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
            if isinstance(data, dict):
                cfg.update(data)

    if os.getenv("NGS_PLATFORM_RUNS_URL"):
        cfg["runs_url"] = os.environ["NGS_PLATFORM_RUNS_URL"].strip()
    if os.getenv("NGS_PLATFORM_BASE_URL"):
        cfg["base_url"] = os.environ["NGS_PLATFORM_BASE_URL"].strip().rstrip("/")

    runs_url = str(cfg.get("runs_url") or "").strip()
    if runs_url and not cfg.get("base_url"):
        u = urlparse(runs_url)
        if u.scheme and u.netloc:
            cfg["base_url"] = f"{u.scheme}://{u.netloc}"

    cfg.setdefault("base_url", "")
    cfg.setdefault("runs_url", "")
    return cfg


def config_ready(cfg: dict) -> bool:
    url = str(cfg.get("runs_url") or "").strip()
    return bool(url and "YOUR_ANALYSIS_PLATFORM" not in url and "example.invalid" not in url)
