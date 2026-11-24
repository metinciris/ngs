from __future__ import annotations
import json, time, traceback
from pathlib import Path


def ts():
    return time.strftime('%Y%m%d_%H%M%S')


def safe_name(text: str) -> str:
    return ''.join(c if c.isalnum() or c in '-_.' else '_' for c in (text or ''))[:80]


def write_diagnostic(page, root: Path, step: str, context: dict, events: list[str] | None = None, exc: Exception | None = None):
    d = root / f"{ts()}_{safe_name(step)}"
    d.mkdir(parents=True, exist_ok=True)
    ctx = dict(context or {})
    try: ctx['url'] = page.url
    except Exception: pass
    try: ctx['title'] = page.title()
    except Exception: pass
    if exc:
        ctx['exception'] = repr(exc)
        ctx['traceback'] = traceback.format_exc()
    (d/'context.json').write_text(json.dumps(ctx, ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        (d/'page.html').write_text(page.content(), encoding='utf-8', errors='replace')
    except Exception as e:
        (d/'page_html_error.txt').write_text(repr(e), encoding='utf-8')
    try:
        txt = page.locator('body').inner_text(timeout=5000)
        (d/'visible_text.txt').write_text(txt, encoding='utf-8', errors='replace')
    except Exception as e:
        (d/'visible_text_error.txt').write_text(repr(e), encoding='utf-8')
    try:
        page.screenshot(path=str(d/'screenshot.png'), full_page=True, timeout=10000)
    except Exception as e:
        (d/'screenshot_error.txt').write_text(repr(e), encoding='utf-8')
    # Iframe/embedded MultiQC tanısı: frame URL, görünür metin ve HTML'i de kaydet.
    try:
        frames=[]
        for idx,fr in enumerate(page.frames):
            meta={'index':idx,'url':fr.url,'name':fr.name}
            try:
                txt=fr.locator('body').inner_text(timeout=2500)
                (d/f'frame_{idx}_visible_text.txt').write_text(txt,encoding='utf-8',errors='replace')
                meta['text_chars']=len(txt)
            except Exception as fe: meta['text_error']=repr(fe)
            try:
                html=fr.content()
                (d/f'frame_{idx}.html').write_text(html,encoding='utf-8',errors='replace')
                meta['html_chars']=len(html)
            except Exception as fe: meta['html_error']=repr(fe)
            frames.append(meta)
        (d/'frames.json').write_text(json.dumps(frames,ensure_ascii=False,indent=2),encoding='utf-8')
    except Exception as e:
        (d/'frames_error.txt').write_text(repr(e),encoding='utf-8')
    # Machine-readable control inventory for AI maintenance.
    try:
        controls=[]
        for idx,fr in enumerate(page.frames):
            try:
                items=fr.locator('button, a, input, [role=\"button\"], [role=\"switch\"], [role=\"tab\"]').evaluate_all("""els => els.slice(0,1200).map((e,i)=>({
                    index:i, tag:e.tagName, role:e.getAttribute('role')||'', type:e.getAttribute('type')||'',
                    name:e.getAttribute('name')||'', title:e.getAttribute('title')||'', ariaLabel:e.getAttribute('aria-label')||'',
                    ariaChecked:e.getAttribute('aria-checked'), checked:('checked' in e)?!!e.checked:null, disabled:('disabled' in e)?!!e.disabled:null,
                    text:(e.innerText||e.value||'').trim().replace(/\\s+/g,' ').slice(0,220),
                    visible:!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length)
                }))""")
                controls.append({'frame_index':idx,'url':fr.url,'controls':items})
            except Exception as ce:
                controls.append({'frame_index':idx,'url':getattr(fr,'url',''),'error':repr(ce)})
        (d/'controls.json').write_text(json.dumps(controls,ensure_ascii=False,indent=2),encoding='utf-8')
    except Exception as e:
        (d/'controls_error.txt').write_text(repr(e),encoding='utf-8')
    if events:
        (d/'events.log').write_text('\n'.join(events[-300:]), encoding='utf-8')
    md = f"""# NGS platformu Collector Tanı Paketi\n\n## Adım\n`{step}`\n\n## Amaç\nBu paket NGS Toplu Collector'ın takıldığı noktayı YZ'nin inceleyebilmesi için otomatik üretildi.\n\n## İncelenecek dosyalar\n- `context.json`: run/sample/POOL ve hata bağlamı\n- `page.html`: o andaki DOM\n- `screenshot.png`: tam sayfa görüntüsü\n- `visible_text.txt`: görünür metin\n- `events.log`: collector'ın son olayları
- `frames.json` + `frame_*.html`: iframe/MultiQC içerikleri\n- `controls.json`: görünür button/link/input/switch envanteri ve checked/aria/title/name durumları\n\n## YZ'den beklenen\n1. Hedef kontrolün güncel DOM selector'ını belirle.\n2. Eski selector'ın neden başarısız olduğunu açıkla.\n3. Mümkünse metin/aria/href tabanlı daha dayanıklı selector öner.\n4. Rastgele fallback tıklaması önermeden güvenli düzeltme üret.\n"""
    (d/'README_AI.md').write_text(md, encoding='utf-8')
    return d
