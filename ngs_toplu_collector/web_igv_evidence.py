# -*- coding: utf-8 -*-
"""Fast, stop-aware structured evidence extractor for the rendered GenNext web IGV.

Policy:
- never search/download/open BAM/BAI/CRAM/BED locally;
- never infer read counts from pixels;
- extract only data that the authenticated GenNext IGV page already rendered/exposes;
- preserve enough bounded DOM diagnostics for AI-assisted extractor repair when a field fails.
"""
from __future__ import annotations

import re
import time
from datetime import datetime

GENERATOR_VERSION = "0.4.35"


class WebIGVStopped(RuntimeError):
    pass


def parse_region(region: str):
    m = re.fullmatch(r"\s*((?:chr)?[A-Za-z0-9_.-]+):(\d[\d,]*)-(\d[\d,]*)\s*", str(region or ""))
    if not m:
        return {"chrom": "", "start": 0, "end": 0, "position": 0}
    a = int(m.group(2).replace(",", "")); b = int(m.group(3).replace(",", ""))
    if a > b:
        a, b = b, a
    return {"chrom": m.group(1), "start": a, "end": b, "position": b}


def _check_stop(stop_cb):
    try:
        if stop_cb and stop_cb():
            raise WebIGVStopped("Web IGV veri toplama kullanıcı tarafından durduruldu.")
    except WebIGVStopped:
        raise
    except Exception:
        pass


def _kv(text: str):
    out = {}
    for line in re.split(r"[\r\n]+", str(text or "")):
        line = re.sub(r"\s+", " ", line).strip()
        if not line or len(line) > 500:
            continue
        m = re.match(r"^([^:=]{1,80})\s*[:=]\s*(.{1,300})$", line)
        if m:
            k = re.sub(r"\s+", "_", m.group(1).strip().lower())
            out.setdefault(k, m.group(2).strip())
    return out


def _numeric_candidates(text: str):
    vals = []
    for m in re.finditer(r"(?i)\b(coverage|depth|value|score|count|reads?)\b\s*[:=]?\s*([0-9][0-9,]*(?:\.[0-9]+)?)", str(text or "")):
        raw = m.group(2).replace(",", "")
        try:
            val = float(raw) if "." in raw else int(raw)
        except Exception:
            continue
        vals.append({"label": m.group(1).lower(), "value": val})
    return vals


_JS_STATE = r'''({targetChr,targetPos}) => {
  const clean=(s)=>(s||'').replace(/\u00a0/g,' ').replace(/\s+/g,' ').trim();
  const host=[...document.querySelectorAll('div.flex-grow-1')].find(el=>el.shadowRoot?.querySelector('.igv-container'));
  const root=host?.shadowRoot||null;
  const out={igv_root:false,genome_build:'',genome_title:'',visible_locus:'',window_size:'',window_size_bp:null,target_in_view:false,target_x_fraction:null,tracks:[],errors:[],dom_signature:{root_children:[],track_fragments:[],popup_classes:[]}};
  if(!root){out.errors.push('IGV shadow root bulunamadı');return out;}
  out.igv_root=true;
  const g=root.querySelector('.igv-current-genome');out.genome_build=clean(g?.textContent||'');out.genome_title=clean(g?.getAttribute('title')||'');
  const inp=root.querySelector('.igv-search-input');out.visible_locus=clean(inp?.value||'');out.window_size=clean(root.querySelector('.igv-windowsize-panel-container')?.textContent||'');
  const lm=out.visible_locus.match(/([^:]+):([\d,]+)-([\d,]+)/);let ls=null,le=null;
  if(lm){ls=Number(lm[2].replace(/,/g,''));le=Number(lm[3].replace(/,/g,''));out.window_size_bp=le-ls+1;if(targetPos&&ls<=targetPos&&targetPos<=le){out.target_in_view=true;out.target_x_fraction=Math.max(0,Math.min(1,(targetPos-ls)/Math.max(1,le-ls)));}}
  function visible(el){if(!el)return false;const cs=getComputedStyle(el),r=el.getBoundingClientRect();return cs.display!=='none'&&cs.visibility!=='hidden'&&Number(cs.opacity||1)!==0&&r.width>0&&r.height>0;}
  function spinner(view){const s=view?.querySelector('.igv-loading-spinner-container');return !!(s&&visible(s));}
  const views=[...root.querySelectorAll('.igv-viewport[data-track-type]')];
  out.tracks=views.map((v,i)=>{const r=v.getBoundingClientRect(),label=clean(v.querySelector('.igv-track-label')?.textContent||v.querySelector('.igv-track-label')?.getAttribute('title')||'');const canv=[...v.querySelectorAll('canvas')];return {index:i,type:v.getAttribute('data-track-type')||'',label,loading:spinner(v),width:Math.round(r.width),height:Math.round(r.height),canvas_count:canv.length,canvas_drawn:canv.some(c=>c.width>10&&c.height>10)};});
  out.dom_signature.root_children=[...root.children].slice(0,30).map(x=>`${x.tagName.toLowerCase()}.${String(x.className||'').replace(/\s+/g,'.')}`);
  out.dom_signature.track_fragments=views.slice(0,12).map(v=>{const h=(v.outerHTML||'').replace(/\s+/g,' ');return h.slice(0,1200);});
  out.dom_signature.popup_classes=[...new Set([...root.querySelectorAll('[class*="popup"],[class*="dialog"],[class*="tooltip"],[class*="popover"]')].map(x=>String(x.className||'')).filter(Boolean))].slice(0,40);
  return out;
}'''


_JS_PROBE = r'''async ({trackType,labelPattern,targetFraction,yFraction,waitMs}) => {
  const sleep=(ms)=>new Promise(r=>setTimeout(r,ms));
  const clean=(s)=>(s||'').replace(/\u00a0/g,' ').replace(/\s+/g,' ').trim();
  const host=[...document.querySelectorAll('div.flex-grow-1')].find(el=>el.shadowRoot?.querySelector('.igv-container'));
  const root=host?.shadowRoot||null;
  const out={attempted:false,track_found:false,track_type:trackType||'',track_label:'',overlay_texts:[],error:''};
  if(!root){out.error='IGV shadow root bulunamadı';return out;}
  const re=labelPattern?new RegExp(labelPattern,'i'):null;
  const views=[...root.querySelectorAll('.igv-viewport[data-track-type]')];
  let view=views.find(v=>(!trackType||v.getAttribute('data-track-type')===trackType)&&(!re||re.test(clean(v.querySelector('.igv-track-label')?.textContent||v.querySelector('.igv-track-label')?.getAttribute('title')||''))));
  if(!view&&trackType)view=views.find(v=>v.getAttribute('data-track-type')===trackType);
  if(!view){out.error='İstenen track bulunamadı';return out;}
  out.track_found=true;out.track_label=clean(view.querySelector('.igv-track-label')?.textContent||view.querySelector('.igv-track-label')?.getAttribute('title')||'');
  const rect=view.getBoundingClientRect();if(rect.width<50||rect.height<8){out.error='Track viewport çok küçük';return out;}
  function visible(el){if(!el)return false;const cs=getComputedStyle(el),r=el.getBoundingClientRect();return cs.display!=='none'&&cs.visibility!=='hidden'&&Number(cs.opacity||1)!==0&&r.width>0&&r.height>0;}
  function overlays(){
    const sel='.igv-menu-popup,.igv-ui-generic-dialog-container,.igv-ui-alert-dialog-container,[class*="popover"],[class*="tooltip"],[class*="popup"]';
    const arr=[];for(const el of root.querySelectorAll(sel)){if(!visible(el))continue;const t=(el.innerText||el.textContent||'').replace(/\u00a0/g,' ').trim();if(t&&t.length>=2&&t.length<=12000)arr.push(t);}return [...new Set(arr)];
  }
  const before=new Set(overlays());const xf=targetFraction==null?.5:Math.max(0,Math.min(1,targetFraction));
  const x=rect.left+Math.max(4,Math.min(rect.width-4,rect.width*xf));const y=rect.top+Math.max(4,Math.min(rect.height-4,rect.height*yFraction));
  const el=root.elementFromPoint?.(x,y)||view.querySelector('canvas')||view;
  out.attempted=true;out.x=Math.round(x);out.y=Math.round(y);
  try{for(const typ of ['pointerdown','mousedown','mouseup','click'])el.dispatchEvent(new MouseEvent(typ,{bubbles:true,composed:true,cancelable:true,view:window,clientX:x,clientY:y,button:0,buttons:typ.includes('down')?1:0}));}
  catch(e){out.error=String(e?.message||e);}
  await sleep(waitMs);
  const after=overlays();const fresh=after.filter(t=>!before.has(t));out.overlay_texts=fresh.length?fresh:after.slice(-4);return out;
}'''


def inspect_web_igv_state(page, region: str):
    target = parse_region(region)
    try:
        raw = page.evaluate(_JS_STATE, {"targetChr": target["chrom"], "targetPos": target["position"]}) or {}
    except Exception as exc:
        raw = {"igv_root": False, "tracks": [], "errors": [str(exc)], "dom_signature": {}}
    raw["requested_region"] = region
    raw["target"] = target
    return raw


def wait_web_igv_ready(page, region: str, timeout_ms=7000, stop_cb=None):
    started = time.monotonic(); last = {}
    while (time.monotonic() - started) * 1000 < max(500, int(timeout_ms)):
        _check_stop(stop_cb)
        last = inspect_web_igv_state(page, region)
        tracks = last.get("tracks") or []
        core = [x for x in tracks if isinstance(x, dict) and x.get("type") == "alignment"]
        if last.get("igv_root") and core and not any(x.get("loading") for x in core):
            return last
        try:
            page.wait_for_timeout(180)
        except Exception:
            time.sleep(.18)
    return last


def set_web_igv_locus(page, region: str, timeout_ms=3500, stop_cb=None):
    target = parse_region(region)
    if not target.get("chrom") or not target.get("position"):
        return {"ok": False, "error": "Geçerli IGV bölgesi yok"}
    pos = int(target["position"]); half = 10
    query = f"{target['chrom']}:{max(1,pos-half)}-{pos+half}"
    try:
        _check_stop(stop_cb)
        inp = page.locator('.igv-search-input').first
        if not inp.count():
            return {"ok": False, "error": "IGV locus search kutusu bulunamadı", "query": query}
        inp.click(timeout=1400); inp.fill(query, timeout=1800); inp.press('Enter', timeout=1400)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "query": query}
    started = time.monotonic(); last = {}
    while (time.monotonic()-started)*1000 < max(800, int(timeout_ms)):
        _check_stop(stop_cb)
        last = inspect_web_igv_state(page, region)
        if last.get("target_in_view"):
            return {"ok": True, "query": query, "visible_locus": last.get("visible_locus") or "", "window_size_bp": last.get("window_size_bp")}
        try: page.wait_for_timeout(160)
        except Exception: time.sleep(.16)
    return {"ok": False, "error": "Hedef lokus süre içinde görünür olmadı", "query": query, "last_state": last}


def _finalize_probe(p):
    if not isinstance(p, dict):
        return {"attempted": False, "raw_text": "", "key_values": {}, "numeric_candidates": [], "exact_pileup": False}
    texts = p.get("overlay_texts") or []
    joined = "\n".join(str(x) for x in texts if x)
    p["raw_text"] = joined
    p["key_values"] = _kv(joined)
    p["numeric_candidates"] = _numeric_candidates(joined)
    p["exact_pileup"] = False
    return p


def probe_track(page, state: dict, kind: str, wait_ms=160, y_fraction=.5, stop_cb=None):
    _check_stop(stop_cb)
    mapping = {
        "variant": ("variant", "SNV|VCF|variant"),
        "target_regions": ("annotation", "Targeted|Padding|Region"),
        "read_depth": ("wig", "Depth|Coverage|Kit Region"),
        "alignment": ("alignment", "BAM|alignment"),
    }
    track_type, label = mapping[kind]
    try:
        p = page.evaluate(_JS_PROBE, {
            "trackType": track_type,
            "labelPattern": label,
            "targetFraction": state.get("target_x_fraction"),
            "yFraction": float(y_fraction),
            "waitMs": max(60, min(700, int(wait_ms))),
        }) or {}
    except Exception as exc:
        p = {"attempted": False, "track_found": False, "overlay_texts": [], "error": str(exc)}
    return _finalize_probe(p)


def build_ai_debug_request(region: str, state: dict, probe_errors: list[str], stage: str, message: str):
    return {
        "request_type": "WEB_IGV_EXTRACTOR_JS_REPAIR",
        "generator_version": GENERATOR_VERSION,
        "failed_stage": stage,
        "error": message,
        "requested_region": region,
        "genome_build": state.get("genome_build") or "",
        "visible_locus": state.get("visible_locus") or "",
        "tracks": state.get("tracks") or [],
        "probe_errors": probe_errors,
        "dom_signature": state.get("dom_signature") or {},
        "instruction": (
            "GenNext web IGV'nin bu DOM/track yapısına göre yalnız başarısız veri alanını okuyan yeni bir Playwright page.evaluate JavaScript öner. "
            "BAM/BAI/BED indirme veya yerel dosya erişimi önerme. Pikselden read/DP/AD/VAF tahmin etme. Mevcut track label/type ve popup DOM yapısını kullan; "
            "kod, alan bulunamazsa null + açıklayıcı error döndürsün ve diğer alanları bozmasın."
        ),
    }


def extract_web_igv_evidence(page, region: str, cfg: dict | None = None, stop_cb=None):
    cfg = cfg or {}; target = parse_region(region)
    _check_stop(stop_cb)
    state = inspect_web_igv_state(page, region)
    reposition = {"ok": bool(state.get("target_in_view")), "method": "already_in_view"}
    if state.get("igv_root") and not state.get("target_in_view") and bool(cfg.get("web_igv_auto_reposition", True)):
        reposition = set_web_igv_locus(page, region, int(cfg.get("web_igv_locus_timeout_ms", 3500)), stop_cb)
        state = wait_web_igv_ready(page, region, int(cfg.get("web_igv_ready_timeout_ms", 7000)), stop_cb)
    probes = {}; probe_errors = []
    wait_ms = int(cfg.get("web_igv_probe_wait_ms", 160))
    if bool(cfg.get("web_igv_probe_popups", True)) and state.get("igv_root"):
        for kind, y in (("variant", .50), ("target_regions", .45), ("read_depth", .50)):
            _check_stop(stop_cb)
            p = probe_track(page, state, kind, wait_ms, y, stop_cb); probes[kind] = p
            if p.get("error"): probe_errors.append(f"{kind}: {p.get('error')}")
            try: page.keyboard.press('Escape')
            except Exception: pass
    read_probe = {"sampled_only": True, "exact_pileup": False, "attempted": 0, "items": []}
    if bool(cfg.get("web_igv_probe_alignment_reads", True)) and state.get("igv_root"):
        count = max(0, min(4, int(cfg.get("web_igv_probe_read_count", 2))))
        ys = (.30, .55, .75, .18)
        for i in range(count):
            _check_stop(stop_cb)
            p = probe_track(page, state, "alignment", wait_ms, ys[i], stop_cb); read_probe["attempted"] += 1
            if p.get("raw_text"): read_probe["items"].append(p)
            if p.get("error"): probe_errors.append(f"alignment[{i}]: {p.get('error')}")
            try: page.keyboard.press('Escape')
            except Exception: pass
    final_state = inspect_web_igv_state(page, region)
    tracks = final_state.get("tracks") or []
    labels = " | ".join(str(x.get("label") or "") for x in tracks if isinstance(x, dict))
    caps = {
        "alignment_track": any(x.get("type") == "alignment" for x in tracks if isinstance(x, dict)),
        "variant_track": any(x.get("type") == "variant" for x in tracks if isinstance(x, dict)),
        "target_padding_track": bool(re.search(r"Targeted|Padding", labels, re.I)),
        "read_depth_track": bool(re.search(r"Read Depth|Coverage", labels, re.I)),
    }
    core_loading = any(x.get("loading") for x in tracks if isinstance(x, dict) and x.get("type") == "alignment")
    data_ready = bool(final_state.get("igv_root") and final_state.get("target_in_view") and caps["alignment_track"] and not core_loading)
    all_errors = list(final_state.get("errors") or []) + probe_errors
    result = dict(final_state)
    result.update({
        "evidence_mode": "web_igv_only",
        "generator_version": GENERATOR_VERSION,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "requested_region": region,
        "target": target,
        "reposition": reposition,
        "track_capabilities": caps,
        "probes": probes,
        "read_probe": read_probe,
        "exact_pileup": False,
        "exact_pileup_status": "not_exposed_by_web_igv",
        "data_ready": data_ready,
        "errors": all_errors,
        "interpretation_boundary": "Only rendered GenNext web IGV DOM/popup data are read. No local BAM/BAI/CRAM/BED access and no pixel-derived read counts.",
    })
    if not data_ready or all_errors:
        if not final_state.get("igv_root"):
            stage, msg = "igv_root", "IGV shadow root bulunamadı"
        elif not final_state.get("target_in_view"):
            stage, msg = "locus", "Hedef lokus görünür alana getirilemedi"
        elif not caps["alignment_track"]:
            stage, msg = "alignment_track", "Alignment/BAM track bulunamadı"
        elif core_loading:
            stage, msg = "alignment_loading", "Alignment track yüklenmesi tamamlanmadı"
        else:
            stage, msg = "popup_probe", "; ".join(all_errors[:5]) or "Bazı web popup alanları alınamadı"
        result["ai_debug_request"] = build_ai_debug_request(region, final_state, probe_errors, stage, msg)
    else:
        result["ai_debug_request"] = None
    return result
