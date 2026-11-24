"""Read-only, FUSION-ONLY GenNext Report Selection (v0.4.80).

This is NOT the ordinary Fusion call list, the table's `None` controls,
DNA Clinical SNVs, or the signed pathology report.  Only checked rows in
Fusion Genes > Fusions > Report selection count as *GenNext report choices*.

No global keyboard, OS clipboard, edits or Generate Report action.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from gennext_report_selection import (
    REPORT_MODAL, SELECTION_BUTTON, SelectionReadError,
    SelectionNavigationUnavailable, _text,
)


FUSION_SCOPE = 'RNA_FUSION_ONLY'


def verify_fusion_url(url, sample):
    parsed = urlparse(str(url or ''))
    m = re.fullmatch(r'/somatic-detail/([^/?#]+)', parsed.path, re.I)
    if not m:
        raise SelectionReadError('RNA Fusion Selection yalniz somatic-detail Fusion ekranindan okunur')
    parts = m.group(1).split('&')
    if (len(parts) != 3
            or parts[0].lower() != str(sample.get('run_uuid') or '').lower()
            or parts[1].lower() != str(sample.get('sample_uuid') or '').lower()
            or parts[2].lower() != 'fusion'):
        raise SelectionReadError('RNA Fusion URL RUN/SAMPLE UUID veya Fusion sekmesi uyusmazligi')
    return True


def verify_fusion_identity(identity, sample):
    verify_fusion_url(identity.get('url'), sample)
    expected = _text(sample.get('patient_id')).upper().replace(' ', '')
    actual = _text(identity.get('patient_id')).upper().replace(' ', '')
    if not expected or expected != actual:
        raise SelectionReadError('Fusion Genes sayfasinda aktif MP dogrulanamadi')
    # GenNext's CSS-module active class is not always present even when
    # the real Fusion Gene table is rendered.  A visible Fusion Gene header
    # inside the Fusion table is independent positive evidence of the view.
    # The run, sample and displayed MP checks above are NEVER bypassed.
    if not (identity.get('fusions_tab_active') is True
            or identity.get('fusion_table_ready') is True):
        raise SelectionReadError('Fusion Genes Fusions gorunumu dogrulanamadi (sekme veya Fusion Gene tablosu hazir degil)')
    return True


def open_fusion_via_run_detail(run_page, sample, timeout_ms=14000):
    """Use GenNext's own Fusion Genes React link; NEVER fabricate a URL."""
    # Trust only a unique and visible GenNext anchor; tolerate renamed CSS
    # classes but never fabricate a Fusion URL or switch a variant selection.
    title = re.compile(r'^\s*Fusion\s+Genes\s*$', re.I)
    links = run_page.locator('a.variant-filtering-button').filter(has_text=title)
    try:
        links.first.wait_for(state='visible', timeout=min(4500,timeout_ms))
    except Exception:
        links = run_page.locator('a').filter(has_text=title)
        try:
            links.first.wait_for(state='visible', timeout=max(250,timeout_ms-4500))
        except Exception as exc:
            raise SelectionNavigationUnavailable('Run Detail Fusion Genes baglantisi hazir degil; kaynak RUNS Sample ID kontrol edilmeli') from exc
    if links.count() != 1 or not links.first.is_visible() or not links.first.is_enabled():
        raise SelectionNavigationUnavailable('Run Detail Fusion Genes baglantisi benzersiz/etkin degil')
    popup = None
    try:
        with run_page.expect_popup(timeout=timeout_ms) as opened:
            links.click(timeout=timeout_ms)
        popup = opened.value
        popup.wait_for_url('**/somatic-detail/*', wait_until='domcontentloaded', timeout=timeout_ms)
        if urlparse(popup.url).netloc.lower() != urlparse(run_page.url).netloc.lower():
            raise SelectionReadError('Fusion Genes baska sunucuya yonlendirildi')
        verify_fusion_url(popup.url, sample)
        return popup
    except SelectionReadError:
        if popup:
            try: popup.close()
            except Exception: pass
        raise
    except Exception as exc:
        if popup:
            try: popup.close()
            except Exception: pass
        raise SelectionNavigationUnavailable('Run Detail Fusion Genes baglantisi yeni sekme acmadi') from exc


def _fusion_pair(raw):
    # Preserve order as shown, never infer direction or breakpoint.
    txt = _text(raw).upper()
    txt = re.sub(r'\s*(?:›|→|>|::)\s*', '::', txt)
    parts = txt.split('::')
    if len(parts) != 2 or any(not re.fullmatch(r'[A-Z][A-Z0-9_.-]{1,40}', p) for p in parts):
        raise SelectionReadError('Selection Fusion gene cifti okunamadi')
    return parts[0], parts[1]


def parse_fusion_selection_data(payload):
    """Fail closed on partial hydration, wrong group, or counter disagreement."""
    if not isinstance(payload, dict) or payload.get('modal_present') is not True:
        raise SelectionReadError('RNA Report selection penceresi bulunamadi')
    match = re.fullmatch(r'(\d+)\s+included', _text(payload.get('header')), re.I)
    if not match:
        raise SelectionReadError('RNA Fusion included sayaci okunamadi')
    included = int(match.group(1))
    groups, rows = payload.get('groups'), payload.get('rows')
    if not isinstance(groups, list) or not isinstance(rows, list):
        raise SelectionReadError('RNA Fusion Selection grup/satir verisi eksik')
    total = total_selected = 0
    for g in groups:
        group = _text(g.get('name'))
        if group.lower() not in ('fusion', 'fusions'):
            raise SelectionReadError('Beklenmeyen RNA rapor secimi grubu: SNV/SV/Fusion karistirilamaz')
        m = re.fullmatch(r'(\d+)\s*/\s*(\d+)', _text(g.get('count')))
        if not m:
            raise SelectionReadError('Fusion grup sayaci okunamadi')
        a,b = map(int, m.groups())
        if a > b: raise SelectionReadError('Fusion grup sayaci tutarsiz')
        total_selected += a
        total += b
    if payload.get('explicit_empty') and (included or total or rows):
        raise SelectionReadError('Bos Fusion Selection ve varyant sayaci celisiyor')
    if not groups and not payload.get('explicit_empty'):
        raise SelectionReadError('RNA Selection yukleniyor; bos sonuc kaniti yok')
    if total == 0 and not payload.get('explicit_empty'):
        raise SelectionReadError('Bos RNA Selection icin acik bos sonuc mesaji gerekli')
    if len(rows) != total:
        raise SelectionReadError('Fusion Selection satirlari eksik veya sanallastirilmis')
    selected, candidates, seen = [], [], set()
    for row in rows:
        if not isinstance(row.get('checked'), bool):
            raise SelectionReadError('RNA checkbox durumu kesin okunamadi')
        g1, g2 = _fusion_pair(row.get('pair'))
        key = 'GenNextFusion|'+g1+'|'+g2
        if key in seen:
            raise SelectionReadError('RNA Fusion Selection tekrarlanan cift')
        seen.add(key)
        callers = _text(row.get('callers'))
        if callers and not re.fullmatch(r'[\w\s,./+-]{1,120}', callers, re.UNICODE):
            raise SelectionReadError('RNA caller bilgisi okunamadi')
        item = {
            'variant_key':key,'fusion':g1+'::'+g2,
            'gene_5prime':g1,'gene_3prime':g2,
            'callers':callers,'origin':_text(row.get('origin')),
        }
        (selected if row['checked'] else candidates).append(item)
    if included != len(selected) or total_selected != len(selected):
        raise SelectionReadError('RNA Fusion dahil edilmis secim sayisi tutarsiz')
    return {'schema':'gennext_fusion_selection_v1','complete':True,
            'clinical_scope':FUSION_SCOPE,'included_count':len(selected),
            'candidate_count':len(candidates),'rows_count':len(rows),
            'selected':selected,'candidates':candidates,
            'source':'GenNext Fusion Genes > Fusions > Selection'}


def _fusion_view_identity(page):
    """Local-only page evidence, NOT a clinical result or Selection assertion."""
    return page.evaluate(r'''() => {
      const norm=s=>String(s||'').replace(/\s+/g,' ').trim().toLowerCase();
      const visible=e=>!!(e && e.getClientRects().length);
      const tab=[...document.querySelectorAll('button')].filter(b =>
        norm(b.textContent)==='fusions' && visible(b));
      const tabActive=tab.length===1 && (
        [...tab[0].classList].some(c=>/module__active__|^(active|selected)$/.test(c)) ||
        tab[0].getAttribute('aria-selected')==='true');
      const header=[...document.querySelectorAll('[role="columnheader"],th')].some(h =>
        visible(h) && norm(h.textContent)==='fusion gene' &&
        !!h.closest('[role="table"],table,.rdt_Table'));
      return {url:location.href,
        patient_id:(document.querySelector('h1.page-heading')?.textContent||'').trim(),
        fusions_tab_active:tabActive,
        fusions_tab_count:tab.length,
        fusion_table_ready:header};
    }''')


def _try_activate_fusions_tab(page, identity):
    """Click the Fusions VIEW tab once, never any report-selection control."""
    if identity.get('fusions_tab_count') != 1:
        return False
    btn=page.locator('button').filter(has_text=re.compile(r'^\s*Fusions\s*$',re.I))
    if btn.count()!=1 or not btn.is_visible() or not btn.is_enabled():
        return False
    try:
        btn.click(timeout=2400)
        return True
    except Exception:
        return False


def _probe(page):
    """Only boolean/count information in exception; no patient/genomic values."""
    try:
        return page.evaluate(r'''() => ({
          fusion_tab: [...document.querySelectorAll('button')].some(b =>
              (b.textContent||'').trim()==='Fusions' &&
              [...b.classList].some(c=>c.includes('active'))),
          selection_buttons: [...document.querySelectorAll('button')].filter(
              b=>(b.textContent||'').trim()==='Selection').length,
          modal_count:document.querySelectorAll('[class*="ReportSelection-module__overlay__"]').length
        })''')
    except Exception: return {'unavailable':True}


def read_fusion_selection(page, sample, stop=None, timeout_ms=18000, keep_modal_open=False,
                          observe=None):
    """Click only the exact Selection modal-opening button, never edit choices.

    Optional passive UI observations are strictly best-effort diagnostics;
    they must not affect or certify a clinical selection.
    """
    def note(stage):
        if callable(observe):
            try:
                observe(page, stage)
            except Exception:
                pass
    verify_fusion_url(page.url, sample)
    note('rna_fusion_enter')
    try:
        page.locator('h1.page-heading').first.wait_for(state='visible',timeout=timeout_ms)
    except Exception as exc:
        raise SelectionReadError('Fusion Genes basligi henuz hazir degil') from exc
    # First confirm the visible MP and exact RUN/SAMPLE UUID.  A stale React
    # page for another patient must never be accepted, clicked or re-labelled.
    identity = _fusion_view_identity(page)
    note('rna_fusion_identity')
    verify_fusion_url(identity.get('url'), sample)
    expected = _text(sample.get('patient_id')).upper().replace(' ', '')
    actual = _text(identity.get('patient_id')).upper().replace(' ', '')
    if not expected or actual != expected:
        raise SelectionReadError('Fusion Genes sayfasinda aktif MP dogrulanamadi')
    # The old code rejected a real Fusion table solely because React did not
    # add a particular __active__ CSS class.  Wait briefly for table hydration;
    # optionally activate only the already-verified Fusions VIEW tab once.
    tab_attempted=False
    for attempt in range(13):
        if stop and stop():raise SelectionReadError('Tarama durduruldu')
        if identity.get('fusions_tab_active') or identity.get('fusion_table_ready'):
            verify_fusion_identity(identity, sample)
            note('rna_fusion_table_ready')
            break
        if attempt>=4 and not tab_attempted:
            tab_attempted=True
            if _try_activate_fusions_tab(page, identity):
                note('rna_fusion_tab_attempt')
        if attempt < 12:
            page.wait_for_timeout(350)
            identity = _fusion_view_identity(page)
            verify_fusion_url(identity.get('url'), sample)
            if _text(identity.get('patient_id')).upper().replace(' ','') != expected:
                raise SelectionReadError('Fusion Genes aktif MP sayfa gecisinde degisti; okuma durduruldu')
    verify_fusion_identity(identity, sample)
    buttons = page.locator(SELECTION_BUTTON)
    try:
        buttons.first.wait_for(state='visible',timeout=min(timeout_ms,6500))
    except Exception:
        # CSS class/icon may change: accept only one visible, exact Selection
        # opening button. Never click Include/Exclude, checkboxes or Analyze.
        fallback=page.locator('button').filter(
            has_text=re.compile(r'^\s*Selection(?:\s+\d+)?\s*$',re.I))
        try:
            if fallback.count()!=1:
                raise SelectionReadError('Fusion Genes Selection dugmesi tekil degil')
            fallback.first.wait_for(state='visible',timeout=max(300,timeout_ms-6500))
            buttons=fallback
        except Exception as exc:
            note('rna_selection_button_missing')
            raise SelectionReadError('Fusion Genes Selection dugmesi hazir degil; '+str(_probe(page))) from exc
    note('rna_selection_button')
    if buttons.count()!=1 or not buttons.is_visible() or not buttons.is_enabled():
        raise SelectionReadError('Fusion Genes Selection dugmesi benzersiz degil; '+str(_probe(page)))
    # The badge number is a separate descendant: "Selection 2" must work.
    own = buttons.evaluate(r"""b => [...b.childNodes].filter(n=>n.nodeType===Node.TEXT_NODE)
      .map(n=>n.textContent||'').join('').replace(/\s+/g,' ').trim().toLowerCase()""")
    if own != 'selection':
        raise SelectionReadError('Fusion Genes Selection dugmesi etiketi belirsiz; '+str(_probe(page)))
    if stop and stop():raise SelectionReadError('Tarama durduruldu')
    try:
        buttons.click(timeout=8000)
    except Exception as exc:
        raise SelectionReadError('Fusion Genes Selection tiklanamadi; '+str(_probe(page))) from exc
    modal = page.locator(REPORT_MODAL)
    try:
        modal.wait_for(state='visible',timeout=timeout_ms)
        note('rna_selection_modal')
        last=None; stable=0; error=''
        for _ in range(34):
            if stop and stop():raise SelectionReadError('Tarama durduruldu')
            payload = modal.evaluate(r'''m => {
              const text=e=>(e?.textContent||'').replace(/\s+/g,' ').trim();
              const sel=(root,frag)=>root.querySelector('[class*="ReportSelection-module__'+frag+'__"]');
              const groups=[...m.querySelectorAll('[class*="ReportSelection-module__group__"]')];
              const rows=[];
              for (const g of groups) {
                const title=sel(g,'groupTitle');
                const groupName=text(title?.cloneNode(true));
                for (const r of g.querySelectorAll('[class*="ReportSelection-module__optionRow__"]')){
                  const cbs=[...r.querySelectorAll('input[type="checkbox"]')].filter(
                    x=>['Include variant in report','Exclude variant in report'].includes(x.getAttribute('aria-label')||''));
                  const cb=cbs.length===1?cbs[0]:null;
                  const lbl=cb?.getAttribute('aria-label')||'';
                  const checked=cb && ((lbl==='Exclude variant in report' && cb.checked===true) ||
                    (lbl==='Include variant in report' && cb.checked===false)) ? cb.checked : null;
                  const meta=text(sel(r,'meta'));
                  rows.push({checked,pair:text(sel(r,'gene')),
                    callers:meta.replace(/^Called by\s*/i,''),
                    origin:text(sel(r,'workgroupSource')||sel(r,'privateSource')||sel(r,'selectionSource'))});
                }
              }
              const empty=sel(m,'emptyState');
              const explicitEmpty=!!(empty && text(empty.querySelector('h5')).toLowerCase()==='no variants selected'
                && [...m.querySelectorAll('.modal-footer button')].some(b=>
                   text(b).toLowerCase()==='continue with no variants'));
              return {modal_present:true,header:text(sel(m,'includedCount')),
                groups:groups.map(g=>{const title=sel(g,'groupTitle');
                    const count=text(sel(g,'count'));
                    return {name:text(title).replace(/\d+\s*\/\s*\d+\s*$/,'').trim(),count};}),
                rows,explicit_empty:explicitEmpty};
            }''')
            signature=json.dumps(payload,sort_keys=True,ensure_ascii=False)
            try:
                parsed=parse_fusion_selection_data(payload)
                stable=stable+1 if signature==last else 1
                if stable>=(12 if payload.get('explicit_empty') else 3):
                    note('rna_selection_stable')
                    return parsed
            except SelectionReadError as exc:
                error=str(exc);stable=0
            last=signature
            page.wait_for_timeout(300)
        raise SelectionReadError('RNA Fusion Selection eksiksiz/kararli okunamadi: '+error)
    except SelectionReadError:raise
    except Exception as exc:
        raise SelectionReadError('RNA Fusion Selection penceresi acilmadi/okunamadi') from exc
    finally:
        note('rna_selection_exit')
        if not keep_modal_open:
            try:
                close=modal.locator('button[aria-label="Close report selection"], button[aria-label="Close"]')
                if close.count():close.first.click(timeout=1300)
            except Exception:pass
