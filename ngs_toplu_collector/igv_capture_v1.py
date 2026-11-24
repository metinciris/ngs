# -*- coding: utf-8 -*-
"""Stable four-image Web IGV capture with bounded page/readiness recovery.

Primary evidence is four independently positioned IGV screenshots. DOM/popup data
is optional and never gates image capture. No local BAM/BAI/CRAM/BED access.
"""
from __future__ import annotations
from gennext_login_hooks import guarded_goto as _ngs_login_goto_v6  # NGS_LOGIN_V6

import hashlib
import io
import json
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

try:
    from PIL import Image, ImageChops, ImageStat
except Exception:  # Pillow is optional; state stability still works without it.
    Image = ImageChops = ImageStat = None

SCHEMA_REQUEST = "ngs_igv_request_v2"
SCHEMA_CAPTURE = "ngs_igv_capture_v2"
SCHEMA_REVIEW = "ngs_igv_review_v2"
CAPTURE_ENGINE_VERSION = "0.4.82"
GENERATOR_VERSION = CAPTURE_ENGINE_VERSION
try:
    APP_VERSION = Path(__file__).with_name("VERSION").read_text(encoding="utf-8-sig").strip()
except OSError:
    APP_VERSION = "unknown"


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def safe_name(value, limit=90):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "NA"))[:limit]


def parse_variant_key(variant_key: str, fallback_region: str = "") -> dict:
    """Return chrom/pos/ref/alt without guessing clinical meaning."""
    raw = str(variant_key or "").strip()
    parts = raw.split("|")
    if parts and parts[0].upper() in {"SNV", "INDEL", "VARIANT"}:
        parts = parts[1:]
    chrom = ref = alt = ""; pos = 0
    if len(parts) >= 4:
        chrom, p, ref, alt = parts[:4]
        try: pos = int(str(p).replace(",", ""))
        except Exception: pos = 0
    if not pos:
        m = re.match(r"\s*(chr[^:]+|[^:]+):(\d+)(?:-(\d+))?\s*$", str(fallback_region or ""), re.I)
        if m:
            chrom = m.group(1)
            try: pos = int(m.group(2))
            except Exception: pos = 0
    if chrom and not chrom.lower().startswith("chr"):
        chrom = "chr" + chrom
    return {"chrom": chrom, "pos": pos, "ref": ref or "", "alt": alt or ""}


def variant_span_bp(ref: str, alt: str) -> int:
    r = "" if ref in ("", "-") else str(ref)
    a = "" if alt in ("", "-") else str(alt)
    return max(1, len(r), len(a), abs(len(r) - len(a)) + 1)


def zoom_windows(chrom: str, pos: int, ref: str = "", alt: str = "") -> list[dict]:
    """Four independent windows. Z4 widens modestly for indels."""
    if not chrom or not int(pos or 0):
        raise ValueError("IGV için kromozom/pozisyon belirlenemedi.")
    span = variant_span_bp(ref, alt)
    z4_half = 15 if span <= 2 else max(15, min(120, 15 + span * 2))
    defs = [("Z1", 500), ("Z2", 150), ("Z3", 50), ("Z4", z4_half)]
    out = []
    for name, half in defs:
        start = max(1, int(pos) - int(half)); end = int(pos) + int(half)
        out.append({"zoom": name, "half_window_bp": int(half), "start": start, "end": end,
                    "window_bp": end - start + 1, "locus": f"{chrom}:{start}-{end}"})
    return out


def popup_type(text: str) -> str:
    """Strict popup classifier: variant/read evidence only when characteristic fields exist."""
    s = clean(text).casefold()
    if not s:
        return "none"
    annotation = ("targeted", "padding", "target region", "kit region")
    variant = ("position", "ref", "alt", "genotype", "allele")
    read = ("read name", "mapping quality", "base quality", "cigar", "flag", "mate")
    if any(x in s for x in read):
        return "read"
    if any(x in s for x in variant):
        return "variant"
    if any(x in s for x in annotation):
        return "annotation"
    return "other"


def _parse_locus(value: str):
    m = re.search(r"([^\s:]+):\s*([\d,]+)\s*-\s*([\d,]+)", str(value or ""))
    if not m:
        return None
    try:
        return m.group(1), int(m.group(2).replace(",", "")), int(m.group(3).replace(",", ""))
    except Exception:
        return None


_STATE_JS = r'''() => {
  const clean=s=>(s||'').replace(/\u00a0/g,' ').replace(/\s+/g,' ').trim();
  const visible=el=>{if(!el)return false;const r=el.getBoundingClientRect(),s=getComputedStyle(el);
    return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity||1)!==0;};
  const hosts=[...document.querySelectorAll('div.flex-grow-1')].filter(x=>x.shadowRoot?.querySelector('.igv-container'));
  const host=hosts.find(visible)||hosts[0]||null;
  const root=host?.shadowRoot||(document.querySelector('.igv-container')?document:null);
  const container=root?.querySelector('.igv-container')||null;
  const body=clean(document.body?.innerText||'').slice(0,8000);
  const errorRe=/the (?:QC|IGV) report could not be loaded|(?:error|failed) (?:loading|to load|to fetch|fetching)(?:[^.\n]{0,60})|internal server error|bad gateway|service unavailable|gateway timeout|ERR_(?:CONNECTION|NAME|INTERNET|NETWORK|TIMED_OUT)[A-Z_]*/i;
  const out={host_found:!!(host||container),shadow_root:!!host?.shadowRoot,container_found:!!container,
    container_visible:visible(container),search_ready:false,alignment_track:false,variant_track:false,
    loading:false,loading_text:/\bLoading IGV\b|IGV\s+y[üu]kleniyor/i.test(body),
    canvas_width:0,canvas_height:0,alignment_painted:false,visible_locus:'',genome_build:'',
    container_rect:null,track_labels:[],page_url:location.href,page_error:'',failure_kind:'',retryable:true};
  const password=[...document.querySelectorAll('input[type="password"]')].some(visible);
  if(password){out.page_error='Platform oturumu giriş ekranına yönlendi';out.failure_kind='authentication';out.retryable=false;}
  else if(errorRe.test(body)){out.page_error=body.match(errorRe)[0];out.failure_kind='platform_error';}
  if(root){
    const alerts=[...root.querySelectorAll('.igv-ui-alert-dialog-container,.igv-viewport-message,[role="alert"],[class*="error"]')].filter(visible);
    for(const el of alerts){const text=clean(el.innerText||el.textContent||'');
      if(errorRe.test(text)){out.page_error=text.match(errorRe)[0];out.failure_kind='track_error';break;}}
  }
  out.loading=out.loading_text||[...document.querySelectorAll('[class*="loading"],[class*="spinner"],[aria-busy="true"]')].some(visible);
  if(!root||!container)return out;
  const cr=container.getBoundingClientRect();out.container_rect={x:cr.x,y:cr.y,width:cr.width,height:cr.height};
  const input=root.querySelector('.igv-search-input');
  out.search_ready=visible(input)&&!input.disabled;out.visible_locus=clean(input?.value||'');
  out.genome_build=clean(root.querySelector('.igv-current-genome')?.textContent||'');
  const vps=[...root.querySelectorAll('.igv-viewport')];
  for(const v of vps){
    if(!visible(v))continue;
    const typ=(v.getAttribute('data-track-type')||'').toLowerCase();
    const label=clean(v.querySelector('.igv-track-label')?.textContent||v.getAttribute('title')||'');
    out.track_labels.push({type:typ,label});
    const alignment=typ==='alignment'||/\bBAM\b|alignment/i.test(label);
    if(alignment)out.alignment_track=true;
    if(typ==='variant'||/SNV|VCF|variant/i.test(label))out.variant_track=true;
    if([...v.querySelectorAll('[class*="loading"],[class*="spinner"],[aria-busy="true"]')].some(visible))out.loading=true;
    if(alignment)for(const c of v.querySelectorAll('canvas')){
      if(!visible(c)||c.width<10||c.height<10)continue;
      out.canvas_width=Math.max(out.canvas_width,Number(c.width||0));out.canvas_height=Math.max(out.canvas_height,Number(c.height||0));
      try{const sample=document.createElement('canvas');sample.width=32;sample.height=16;
        const ctx=sample.getContext('2d');ctx.drawImage(c,0,0,32,16);const pixels=ctx.getImageData(0,0,32,16).data;
        for(let i=3;i<pixels.length;i+=4)if(pixels[i]>0){out.alignment_painted=true;break;}
      }catch(e){out.canvas_probe_error=true;}
    }
  }
  const spinner=[...root.querySelectorAll('[class*="loading"],[class*="spinner"],[aria-busy="true"]')].some(visible);
  out.loading=out.loading||spinner; return out;
}'''


class IGVStopped(RuntimeError):
    pass


def _check_stop(stop_cb):
    if stop_cb and stop_cb():
        raise IGVStopped('IGV görüntü toplama kullanıcı tarafından durduruldu.')


def _pause(page, milliseconds, stop_cb=None):
    remaining = max(0, int(milliseconds))
    while remaining:
        _check_stop(stop_cb)
        step = min(100, remaining)
        page.wait_for_timeout(step)
        remaining -= step
    _check_stop(stop_cb)


def _same_url(actual, expected):
    if not expected:
        return True
    a, b = urlsplit(str(actual or '')), urlsplit(str(expected or ''))
    return (a.scheme.casefold(), a.netloc.casefold(), unquote(a.path).rstrip('/'),
            unquote(a.query), unquote(a.fragment)) == (
            b.scheme.casefold(), b.netloc.casefold(), unquote(b.path).rstrip('/'),
            unquote(b.query), unquote(b.fragment))


def _identity_error(state, expected_url='', expected_build=''):
    if expected_url and not _same_url(state.get('page_url'), expected_url):
        return 'IGV sayfası istenen teknik kayıt adresinden farklı', 'identity_mismatch'
    from igv_workflow_v0481 import canonical_build
    observed = canonical_build(state.get('genome_build'))
    expected = canonical_build(expected_build)
    if observed and expected and observed.casefold() != expected.casefold():
        return 'IGV genom sürümü istenen genom sürümünden farklı', 'genome_mismatch'
    return '', ''


def inspect_state(page, target_pos: int) -> dict:
    try:
        state = page.evaluate(_STATE_JS) or {}
    except Exception as exc:
        return {"host_found": False, "shadow_root": False, "alignment_track": False, "variant_track": False,
                "loading": True, "canvas_width": 0, "canvas_height": 0, "visible_locus": "", "error": str(exc),
                "page_url": str(getattr(page,'url','') or '')}
    loc = _parse_locus(state.get("visible_locus") or "")
    state["target_in_view"] = False; state["target_center_fraction"] = None; state["window_bp"] = None
    if loc:
        _c, a, b = loc
        if b < a: a, b = b, a
        state["window_bp"] = b - a + 1
        state["target_in_view"] = a <= int(target_pos) <= b
        if b > a:
            state["target_center_fraction"] = (int(target_pos) - a) / float(b - a)
    return state


def wait_for_viewer(page, target_pos, cfg, stop_cb=None, *, expected_url='', expected_build='', progress_cb=None):
    """Wait for the actual input/container before spending a locus attempt."""
    timeout = max(1000, min(60000, int(cfg.get('igv_viewer_ready_timeout_ms',60000))))
    started = time.monotonic(); last = {}; announced = -1
    while (time.monotonic()-started)*1000 < timeout:
        _check_stop(stop_cb)
        last = inspect_state(page,target_pos)
        if last.get('page_error'):
            last.update(viewer_ready=False,ready_errors=[last['page_error']])
            return last
        message, kind = _identity_error(last,expected_url,expected_build)
        if message and last.get('page_url') not in ('','about:blank'):
            last.update(viewer_ready=False,ready_errors=[message],failure_kind=kind,retryable=False)
            return last
        if last.get('container_visible') and last.get('search_ready') and not last.get('loading') and not message:
            last['viewer_ready'] = True
            return last
        elapsed = int(time.monotonic()-started)
        if progress_cb and elapsed != announced:
            progress_cb(f'IGV sayfası yükleniyor · {elapsed} sn'); announced = elapsed
        _pause(page, min(200, max(1,timeout-(time.monotonic()-started)*1000)), stop_cb)
    last.update(viewer_ready=False,ready_errors=['IGV görüntüleyicisi süre içinde hazır olmadı'],
                failure_kind='viewer_timeout',retryable=True)
    return last


def open_igv_viewer(page, url, target_pos, cfg, stop_cb=None, *, expected_build='', progress_cb=None, navigation_history=None):
    """At most two navigations, always to the same frozen request URL.

    The caller reuses navigation_history for stage recovery, so opening and
    later errors share one retry budget. A new explicit user attempt gets a
    fresh budget; previously completed images remain independent of it.
    """
    history = navigation_history if navigation_history is not None else []
    last = {'viewer_ready':False,'ready_errors':['IGV sayfa yeniden deneme sınırı doldu'],
            'failure_kind':'retry_limit','retryable':False}
    nav_timeout = max(1000,min(60000,int(cfg.get('igv_v436_nav_timeout_ms',20000))))
    while len(history) < 2:
        _check_stop(stop_cb)
        attempt = {'attempt':len(history)+1,'requested_url':url}
        if history:
            if progress_cb: progress_cb('IGV aynı kayıt için yeniden yükleniyor · 2/2')
            _pause(page,max(100,min(2000,int(cfg.get('igv_page_retry_delay_ms',1000)))),stop_cb)
        history.append(attempt)
        try:
            response = _ngs_login_goto_v6(page, url,wait_until='commit',timeout=nav_timeout)
            status = int(response.status) if response is not None else 0
            attempt['http_status'] = status
            if status >= 400:
                last = {'viewer_ready':False,'ready_errors':[f'IGV platform HTTP {status} hatası'],
                        'failure_kind':'http_error','http_status':status,
                        'retryable':status in (408,425,429) or status>=500}
            else:
                last = wait_for_viewer(page,target_pos,cfg,stop_cb,expected_url=url,
                                       expected_build=expected_build,progress_cb=progress_cb)
        except IGVStopped:
            raise
        except Exception as exc:
            # The previous document can retain the same URL after a failed
            # reload. It is not proof that this navigation ever committed.
            last = {'viewer_ready':False,'ready_errors':['IGV navigasyonu tamamlanmadı: '+str(exc)],
                    'failure_kind':'navigation_error','retryable':True}
        attempt.update(ready=bool(last.get('viewer_ready')),failure_kind=last.get('failure_kind') or '',
                       errors=list(last.get('ready_errors') or []))
        if last.get('viewer_ready') or not last.get('retryable',True):
            return last
    return last


def set_locus(page, locus: str):
    inp = page.locator('.igv-search-input').first
    if not inp.count():
        raise RuntimeError("IGV locus search kutusu bulunamadı.")
    inp.click(timeout=1800); inp.fill(locus, timeout=2200); inp.press('Enter', timeout=1800)


def _capture_panel_bytes(page) -> bytes:
    loc = page.locator('.igv-container').first
    if loc.count():
        return loc.screenshot(timeout=5000, animations='disabled')
    state = page.evaluate(_STATE_JS) or {}
    rect = state.get('container_rect') or {}
    if not rect or float(rect.get('width') or 0) <= 0 or float(rect.get('height') or 0) <= 0:
        raise RuntimeError("IGV panel ekran alanı bulunamadı.")
    return page.screenshot(clip={"x": float(rect['x']), "y": float(rect['y']), "width": float(rect['width']), "height": float(rect['height'])}, animations='disabled')


def _image_delta(a: bytes, b: bytes) -> float | None:
    if not a or not b:
        return None
    if Image is None or ImageChops is None or ImageStat is None:
        return 0.0 if hashlib.sha256(a).digest() == hashlib.sha256(b).digest() else None
    try:
        ia = Image.open(io.BytesIO(a)).convert('L').resize((96, 64))
        ib = Image.open(io.BytesIO(b)).convert('L').resize((96, 64))
        d = ImageChops.difference(ia, ib)
        mean = ImageStat.Stat(d).mean[0]
        return float(mean) / 255.0
    except Exception:
        return None


def _ready_core(state: dict, target_pos: int, locus='', expected_url='', expected_build='') -> tuple[bool, list[str]]:
    misses = []
    if state.get('page_error'): misses.append(state['page_error'])
    identity_error, kind = _identity_error(state,expected_url,expected_build)
    if identity_error:
        misses.append(identity_error); state.update(failure_kind=kind,retryable=False)
    if not state.get('host_found'): misses.append('IGV host yok')
    if not state.get('container_visible'): misses.append('IGV paneli görünür değil')
    if not state.get('alignment_track'): misses.append('Alignment track yok')
    if int(state.get('canvas_width') or 0) <= 0 or int(state.get('canvas_height') or 0) <= 0: misses.append('Canvas boş')
    if not state.get('alignment_painted'): misses.append('Alignment canvas render edilmedi')
    if not state.get('target_in_view'): misses.append('Hedef görünür locus içinde değil')
    expected, observed = _parse_locus(locus), _parse_locus(state.get('visible_locus'))
    if expected and observed:
        chrom = lambda value: re.sub(r'^chr','',str(value),flags=re.I).casefold()
        if chrom(expected[0]) != chrom(observed[0]):
            misses.append('Görüntülenen kromozom istenen kromozom değil')
            state.update(failure_kind='locus_mismatch',retryable=False)
        want = abs(expected[2]-expected[1])+1
        got = abs(observed[2]-observed[1])+1
        if abs(want-got) > max(2, want*0.02):
            misses.append('İstenen zoom aralığı henüz render edilmedi')
    frac = state.get('target_center_fraction')
    if frac is None or not (0.30 <= float(frac) <= 0.70): misses.append('Hedef yaklaşık merkezde değil')
    if state.get('loading'): misses.append('IGV loading devam ediyor')
    return not misses, misses


def wait_ready_and_stable(page, locus: str, target_pos: int, cfg: dict, stop_cb=None, *, expected_url='', expected_build='', progress_cb=None) -> dict:
    timeout_ms = max(2500, int(cfg.get('igv_v436_stage_timeout_ms', 12000)))
    stable_wait = max(350, min(900, int(cfg.get('igv_v436_stable_wait_ms', 500))))
    stable_delta = float(cfg.get('igv_v436_stable_delta', 0.012))
    poll = max(120, min(500, int(cfg.get('igv_v436_poll_ms', 220))))
    started = time.monotonic(); last = {}; last_misses = []; announced = -1
    while (time.monotonic() - started) * 1000 < timeout_ms:
        _check_stop(stop_cb)
        last = inspect_state(page, target_pos)
        ok, last_misses = _ready_core(last,target_pos,locus,expected_url,expected_build)
        if last.get('page_error') or not last.get('retryable',True):
            break
        if ok:
            try:
                a = _capture_panel_bytes(page)
                _pause(page,stable_wait,stop_cb)
                b = _capture_panel_bytes(page)
                st2 = inspect_state(page, target_pos)
                ok2, misses2 = _ready_core(st2,target_pos,locus,expected_url,expected_build)
                delta = _image_delta(a, b)
                same_locus = clean(last.get('visible_locus')) == clean(st2.get('visible_locus'))
                stable = bool(ok2 and same_locus and delta is not None and delta <= stable_delta)
                if stable:
                    st2['stable'] = True; st2['stable_delta'] = delta; st2['stability_method'] = 'panel_screenshot_pair'
                    # Keep the exact second frame bracketed by readiness checks.
                    # A later third screenshot could catch a new loading/error page.
                    st2['_stable_image_bytes'] = b
                    return st2
                last = st2; last_misses = misses2 + ([] if same_locus else ['Locus iki kontrolde değişti'])
            except IGVStopped:
                raise
            except Exception as exc:
                last_misses = [f'Stabilite görüntüsü alınamadı: {exc}']
        elapsed = int(time.monotonic()-started)
        if progress_cb and elapsed != announced:
            progress_cb(f'IGV izleri/render bekleniyor · {elapsed} sn'); announced = elapsed
        _pause(page,poll,stop_cb)
    last['stable'] = False; last['ready_errors'] = last_misses
    return last


def capture_stage(page, stage: dict, target_pos: int, out_path: Path, cfg: dict, stop_cb=None, *, expected_url='', expected_build='', progress_cb=None) -> dict:
    attempts = max(1, min(3, int(cfg.get('igv_v436_stage_attempts', 3))))
    errors = []; state = {}; attempt = 0
    # Late shadow DOM is a page-loading state, not a failed locus attempt.
    state = wait_for_viewer(page,target_pos,cfg,stop_cb,expected_url=expected_url,
                            expected_build=expected_build,progress_cb=progress_cb)
    if not state.get('viewer_ready'):
        errors.extend(state.get('ready_errors') or ['IGV sayfası hazır değil'])
        attempts = 0
    for attempt in range(1, attempts + 1):
        _check_stop(stop_cb)
        try:
            set_locus(page, stage['locus'])
            # Give the Enter handler a browser event turn to begin its loading state.
            _pause(page,120,stop_cb)
            state = wait_ready_and_stable(page,stage['locus'],target_pos,cfg,stop_cb,
                                          expected_url=expected_url,expected_build=expected_build,progress_cb=progress_cb)
            if state.get('stable'):
                _check_stop(stop_cb)
                data = state.pop('_stable_image_bytes')
                out_path.parent.mkdir(parents=True, exist_ok=True); out_path.write_bytes(data)
                return {"zoom": stage['zoom'], "status": "complete", "attempt": attempt, "filename": out_path.name,
                        "path": str(out_path), "locus": stage['locus'], "window_bp": stage['window_bp'],
                        "visible_locus": state.get('visible_locus') or '', "variant_track": bool(state.get('variant_track')),
                        "observed_genome_build":state.get('genome_build') or '',
                        "stable_delta": state.get('stable_delta'), "captured_at": datetime.now().isoformat(timespec='seconds'),
                        "capture_engine_version": CAPTURE_ENGINE_VERSION, "errors": errors}
            errors.append('; '.join(state.get('ready_errors') or ['Render stabil olmadı']))
            if state.get('page_error') or not state.get('retryable',True):
                break
        except IGVStopped:
            raise
        except Exception as exc:
            errors.append(str(exc))
        if attempt < attempts:
            _pause(page,max(100,min(2000,int(cfg.get('igv_v436_retry_delay_ms',450)))),stop_cb)
    return {"zoom": stage['zoom'], "status": "error", "attempt": attempt, "filename": out_path.name,
            "path": str(out_path), "locus": stage['locus'], "window_bp": stage['window_bp'], "errors": errors,
            "failure_kind":state.get('failure_kind') or 'render_timeout',"retryable":state.get('retryable',True),
            "captured_at": datetime.now().isoformat(timespec='seconds')}


def _popup_probe(page, kind: str, target_pos: int) -> dict | None:
    """Optional bonus. Wrong annotation popups are explicitly rejected."""
    try:
        from web_igv_evidence import inspect_web_igv_state, probe_track
        state = inspect_web_igv_state(page, f"chr1:{target_pos}-{target_pos}") or {}
        # target_x_fraction is viewport-relative and does not depend on the fake chr above.
        y = .50 if kind == 'variant' else .55
        p = probe_track(page, state, kind, wait_ms=180, y_fraction=y) or {}
        text = clean(p.get('raw_text') or '\n'.join(p.get('overlay_texts') or []))
        typ = popup_type(text)
        expected = 'variant' if kind == 'variant' else 'read'
        try: page.keyboard.press('Escape')
        except Exception: pass
        if typ != expected:
            return None
        return {"popup_type": typ, "raw_text": text[:12000], "key_values": p.get('key_values') or {}}
    except Exception:
        return None


def optional_popups(page, target_pos: int, cfg: dict) -> dict:
    if not bool(cfg.get('igv_v436_popup_bonus', True)):
        return {"variant_popup": None, "read_popup": None}
    return {"variant_popup": _popup_probe(page, 'variant', target_pos), "read_popup": _popup_probe(page, 'alignment', target_pos)}


def load_capture_state(path: Path) -> dict:
    try:
        if path.exists():
            d = json.loads(path.read_text(encoding='utf-8'))
            if d.get('schema') in (SCHEMA_CAPTURE, "ngs_igv_capture_v1"):
                return d
    except Exception:
        pass
    return {}


def write_capture_state(path: Path, data: dict):
    import os
    import tempfile
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink()


def capture_status(capture: dict) -> str:
    images = capture.get('images') or []
    n = len({x.get('zoom') for x in images if isinstance(x, dict) and x.get('zoom') in ('Z1','Z2','Z3','Z4') and x.get('status') == 'complete' and Path(str(x.get('path') or '')).is_file()})
    if n == 4: return 'complete'
    if n > 0: return 'partial'
    return 'error' if any((x or {}).get('status') == 'error' for x in images if isinstance(x, dict)) else 'pending'


def zoom_summary(capture: dict) -> str:
    by = {str(x.get('zoom')): x for x in (capture.get('images') or []) if isinstance(x, dict)}
    return '  '.join(f"{'✓' if by.get(z,{}).get('status')=='complete' and Path(str(by.get(z,{}).get('path') or '')).is_file() else '✕'} {z}" for z in ('Z1','Z2','Z3','Z4'))
