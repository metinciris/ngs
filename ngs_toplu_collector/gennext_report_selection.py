"""Read-only GenNext Analyzed + Report Selection snapshot (v0.4.78).

Never toggles the Analyzed switch, report checkboxes, Include all shared
candidates, Continue, or Generate Report. Selection is a *clinical SNV/indel*
selection, not a whole-panel or signed/electronic report conclusion.
"""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlparse, parse_qs

REPORT_MODAL = '[class*="ReportSelection-module__overlay__"]'
OPTION_ROW = '[class*="ReportSelection-module__optionRow__"]'
# The live GenNext button carries an icon but no aria-label.
SELECTION_BUTTON = 'button.btn.btn-light-primary:has(> i.fa-list)'


class SelectionReadError(ValueError):
    """Reading the UI cannot safely justify a positive/negative conclusion."""


class SelectionNavigationUnavailable(SelectionReadError):
    """The Run Detail navigation link did not open; direct URL may be tried."""


def _text(s):
    return re.sub(r'\s+', ' ', str(s or '')).strip()


def verify_run_detail(identity, sample):
    """Validate URL/sample UUID and selected sample label against *this* record."""
    url = urlparse(str(identity.get('url') or ''))
    run = str(sample.get('run_uuid') or '').lower()
    su = str(sample.get('sample_uuid') or '').lower()
    if '/run-detail-somatic/' not in url.path.lower():
        raise SelectionReadError('Beklenmeyen Run Detail sayfası')
    if run and url.path.rstrip('/').rsplit('/', 1)[-1].lower() != run:
        raise SelectionReadError('Run UUID uyuşmuyor')
    sample_in_url = (parse_qs(url.query).get('sampleUuid') or [''])[0].lower()
    if not su or sample_in_url != su:
        raise SelectionReadError('Run Detail sampleUuid uyuşmuyor')
    checked = identity.get('analyzed')
    if not isinstance(checked, bool) or identity.get('switch_count') != 1:
        raise SelectionReadError('Analyzed anahtarı açık/kapalı doğrulanamadı')
    active = _text(identity.get('active_sample')).upper().replace(' ', '')
    expected = _text(sample.get('patient_id')).upper().replace(' ', '')
    if not active or not expected or active != expected:
        raise SelectionReadError(f'GenNext aktif MP uyuşmazlığı ({active or "?"} / {expected or "?"})')
    return checked


def verify_clinical_url(url, sample):
    parsed=urlparse(str(url or ''))
    m=re.fullmatch(r'/somatic-detail-clinic/([^/?#]+)',parsed.path,re.I)
    if not m:
        raise SelectionReadError('Selection yalniz Clinical SNVs sayfasindan okunur; ham SNV ekranindan degil')
    parts=m.group(1).split('&')
    if len(parts)!=3 or parts[0].lower()!=str(sample.get('run_uuid') or '').lower() or parts[1].lower()!=str(sample.get('sample_uuid') or '').lower() or parts[2].lower()!='snv':
        raise SelectionReadError('Clinical SNVs URL run/sampleUuid uyuşmazlığı')
    return True


def verify_clinical_identity(identity, sample):
    """Prevent saving an older React page for a different MP despite a matching URL."""
    verify_clinical_url(identity.get('url'), sample)
    displayed=_text(identity.get('patient_id')).upper().replace(' ', '')
    expected=_text(sample.get('patient_id')).upper().replace(' ', '')
    if not displayed or not expected or displayed != expected:
        raise SelectionReadError('Clinical SNVs sayfasinda aktif MP dogrulanamadi; kayit yapilmadi')
    return True


def open_clinical_via_run_detail(run_page, sample, timeout_ms=13000):
    """Open GenNext's *own* Clinical SNVs link (React SPA route) read-only.

    The saved Run Detail MHTML has an anchor with no href and target=_blank;
    its onClick creates the correct clinical page only after React hydration.
    Guessing the URL via page.goto can leave a shell without the Selection UI.
    """
    # GenNext CSS modules can change class names. Use a strict visible,
    # unique GenNext anchor as a fallback; NEVER synthesize a clinical URL.
    title=re.compile(r'^\s*Clinical\s+SNVs\s*/\s*Short\s+INDEL\s*$',re.I)
    links=run_page.locator('a.variant-filtering-button').filter(has_text=title)
    try:
        links.first.wait_for(state='visible',timeout=min(4500,timeout_ms))
    except Exception:
        links=run_page.locator('a').filter(has_text=title)
        try:
            links.first.wait_for(state='visible',timeout=max(250,timeout_ms-4500))
        except Exception as exc:
            raise SelectionNavigationUnavailable('Run Detail Clinical SNVs baglantisi hazir degil; kaynak RUNS ve Sample ID yeniden kontrol edilmeli') from exc
    if links.count()!=1 or not links.first.is_visible() or not links.first.is_enabled():
        raise SelectionNavigationUnavailable('Run Detail Clinical SNVs baglantisi tekil/etkin degil')
    popup=None
    try:
        with run_page.expect_popup(timeout=timeout_ms) as opened:
            links.click(timeout=timeout_ms)
        popup=opened.value
        popup.wait_for_url('**/somatic-detail-clinic/*',wait_until='domcontentloaded',
                           timeout=timeout_ms)
    except Exception as exc:
        if popup:
            try:popup.close()
            except Exception:pass
        raise SelectionNavigationUnavailable('Run Detail Clinical SNVs baglantisi yeni sekme acmadi') from exc
    try:
        if urlparse(run_page.url).netloc.lower()!=urlparse(popup.url).netloc.lower():
            raise SelectionReadError('Clinical SNVs penceresi farkli sunucuya yonlendirildi')
        verify_clinical_url(popup.url,sample)
    except SelectionReadError:
        try:popup.close()
        except Exception:pass
        raise
    return popup


def _clinical_page_probe(page):
    """Non-PHI failure clue for logs: no HTML, names or mutation content."""
    try:
        return page.evaluate(r"""() => ({
          heading: !!document.querySelector('h1.page-heading'),
          fa_list: document.querySelectorAll('button:has(> i.fa-list)').length,
          all_buttons: document.querySelectorAll('button').length,
          modal: document.querySelectorAll('[class*="ReportSelection-module__overlay__"]').length,
          login: !!document.querySelector('#kt_sign_in_submit, input[type="password"]')
        })""")
    except Exception:
        return {'dom':'unavailable'}


def _variant_key(v):
    # Exact GenNext displayed genomic allele; never infer a transcript/build.
    genomic = _text(v.get('genomic'))
    m=re.search(r'\bchr([0-9A-Za-z_]+)\s*:\s*(\d+)\s+([ACGTN-]+)\s*>\s*([ACGTN-]+)\b',genomic,re.I)
    if m:
        return 'chr{}|{}|{}|{}'.format(m.group(1).upper(),m.group(2),m.group(3).upper(),m.group(4).upper())
    # Fall back to a GenNext-specific identity, NEVER conflate with raw VCF keys.
    return 'GenNext|{}|{}|{}|{}'.format(_text(v.get('gene')).upper(),_text(v.get('protein')),
                                       _text(v.get('cdna')),genomic)


def parse_selection_data(payload):
    """Strictly compare rendered rows with UI totals; fail closed on ambiguity."""
    if not isinstance(payload,dict) or not payload.get('modal_present'):
        raise SelectionReadError('Report selection penceresi bulunamadı')
    header=_text(payload.get('header'))
    m=re.fullmatch(r'(\d+)\s+included',header,re.I)
    if not m:
        raise SelectionReadError('Report inclusion sayacı doğrulanamadı')
    expected_included=int(m.group(1))
    groups=payload.get('groups') or []
    raw_rows=payload.get('rows') or []
    if not isinstance(raw_rows,list) or not isinstance(groups,list):
        raise SelectionReadError('Selection grup/satır bilgisi okunamadı')
    expected_total=0
    expected_group_included=0
    for group in groups:
        match=re.fullmatch(r'(\d+)\s*/\s*(\d+)',_text(group.get('count')))
        if not match:
            raise SelectionReadError('Selection grup sayacı okunamadı')
        a,b=map(int,match.groups())
        if a>b:raise SelectionReadError('Selection grup sayacı tutarsız')
        expected_total+=b
        expected_group_included+=a
    if payload.get('explicit_empty') and (expected_included or expected_total or raw_rows):
        # The actual GenNext empty view cannot coexist with selected/candidate rows.
        # Treat an inconsistent/react-updating modal as uncertain, never negative.
        raise SelectionReadError('Bos Selection mesaji ile varyant/sayac celisiyor')
    if expected_total==0 and not payload.get('explicit_empty'):
        # A 0/0 group or a half-hydrated modal may have no rows yet.
        raise SelectionReadError('Bos Selection icin acik bos sonuc isareti bekleniyor')
    if len(raw_rows)!=expected_total:
        # This also blocks false negatives when GenNext virtualizes hidden rows.
        raise SelectionReadError(f'Görünen Selection satırı eksik: {len(raw_rows)}/{expected_total}')
    if not groups and not (payload.get('explicit_empty') and expected_included==0):
        raise SelectionReadError('Selection yükleniyor olabilir; açıkça boş sonuç kanıtı yok')
    selected=[];candidates=[];seen=set()
    for row in raw_rows:
        checked=row.get('checked')
        if not isinstance(checked,bool):
            raise SelectionReadError('Selection checkbox durumu belirsiz')
        gene=_text(row.get('gene')).upper(); protein=_text(row.get('protein'))
        if not gene or not re.fullmatch(r'[A-Z0-9_.-]{2,30}',gene):
            raise SelectionReadError('Selection varyant gen adı okunamadı')
        v={
           'variant_key':'', 'gene':gene, 'protein':protein, 'cdna':_text(row.get('cdna')),
           'exon':_text(row.get('exon')),'genomic':_text(row.get('genomic')),
           'vaf':_text(row.get('vaf')),'ad_dp':_text(row.get('ad_dp')),
           'tier':_text(row.get('tier')),'filter':_text(row.get('filter')),
           'consequence':_text(row.get('consequence')),'group':_text(row.get('group')),
           'origin':_text(row.get('origin')),
        }
        v['variant_key']=_variant_key(v)
        if v['variant_key'] in seen:
            raise SelectionReadError('Selection içinde yinelenen varyant anahtarı')
        seen.add(v['variant_key'])
        (selected if checked else candidates).append(v)
    if expected_included!=len(selected) or expected_group_included!=len(selected):
        raise SelectionReadError(f'Seçim sayısı tutarsız: başlık={expected_included}, grup={expected_group_included}, işaretli={len(selected)}')
    return {'schema':'gennext_report_selection_v1','complete':True,'included_count':len(selected),
            'candidate_count':len(candidates),'rows_count':len(raw_rows),
            'selected':selected,'candidates':candidates,
            'clinical_scope':'SNV_INDEL_ONLY', 'source':'GenNext Clinical SNVs > Selection'}


def snapshot_hash(snapshot):
    relevant={k:snapshot.get(k) for k in ('analyzed','included_count','candidate_count','selected','candidates','clinical_scope')}
    return hashlib.sha256(json.dumps(relevant,sort_keys=True,ensure_ascii=False).encode('utf-8')).hexdigest()


def read_analyzed(page, sample, timeout_ms=14000):
    """Run Detail: inspect the sole solved switch and exact active MP (no clicks)."""
    try:
        page.wait_for_selector('input[value="runIsSolved"]',state='attached',timeout=timeout_ms)
    except Exception as exc:
        raise SelectionReadError('Run Detail sayfasinda Analyzed anahtari henuz hazir degil; sonuc uretilemedi') from exc
    # React can briefly display the previously selected MP after the switch
    # mounts; permit a bounded hydration interval, then verify strict MP+UUID.
    expected=_text(sample.get('patient_id')).upper().replace(' ','')
    try:
        page.wait_for_function(r'''expected => {
          const el=document.querySelector('.dropdown-item.active span.fw-bold');
          return !!el && (el.textContent||'').replace(/\s+/g,'').toUpperCase()===expected;
        }''', arg=expected,timeout=min(timeout_ms,4200),polling=300)
    except Exception:
        pass  # A composite/different MP still fails verification below.
    data=page.evaluate(r'''() => {
      const sw=[...document.querySelectorAll('input[value="runIsSolved"]')];
      const el=document.querySelector('.dropdown-item.active span.fw-bold');
      return {url:location.href,switch_count:sw.length,
              analyzed:sw.length===1?sw[0].checked:null,
              active_sample:el?(el.textContent||'').trim():''};
    }''')
    return verify_run_detail(data,sample)


def read_selection(page,sample,stop=None,timeout_ms=19000):
    """Open ONLY Selection modal and inspect checkboxes. No report state is changed."""
    verify_clinical_url(page.url,sample)
    try:
        page.locator('h1.page-heading').first.wait_for(state='visible',timeout=timeout_ms)
    except Exception as exc:
        raise SelectionReadError('Clinical SNVs vaka basligi yuklenmedi; '
                                 +str(_clinical_page_probe(page))) from exc
    identity=page.evaluate(r"""() => ({url:location.href,
      patient_id:(document.querySelector('h1.page-heading')?.textContent||'').trim()})""")
    verify_clinical_identity(identity,sample)
    # The real GenNext control has a numeric badge inside the button:
    # <button><i class="fa fa-list"></i>Selection<span class="badge">3</span></button>.
    # innerText / accessible name therefore becomes "Selection3". Never compare
    # the whole button text against exactly "Selection".
    try:
        page.wait_for_function(r"""() => {
          const visible = e => {
            const r=e.getBoundingClientRect(), cs=getComputedStyle(e);
            return r.width>0 && r.height>0 && cs.visibility!=='hidden' && cs.display!=='none';
          };
          const matches=[...document.querySelectorAll('button.btn.btn-light-primary')].filter(b => {
            if (!visible(b) || b.disabled || !b.querySelector(':scope > i.fa-list')) return false;
            const own=[...b.childNodes].filter(n=>n.nodeType===Node.TEXT_NODE)
              .map(n=>n.textContent||'').join('').replace(/\s+/g,' ').trim();
            return own.toLowerCase()==='selection';
          });
          return matches.length===1;
        }""", timeout=timeout_ms, polling=350)
    except Exception as exc:
        raise SelectionReadError('Selection dugmesi hazir olmadi (ikon/metin, tekil, gorunur, aktif); '
                                 +str(_clinical_page_probe(page))) from exc
    button=page.locator(SELECTION_BUTTON)
    # Scoping with the icon selector excludes unrelated buttons and accepts
    # the live badge count (0, 2, 3, ...). Never use exact=True for its name.
    if button.count()!=1 or not button.is_visible() or not button.is_enabled():
        raise SelectionReadError('Selection dugmesi tekil olarak dogrulanamadi; '
                                 +str(_clinical_page_probe(page)))
    own_text=button.evaluate(r"""b => [...b.childNodes]
      .filter(n=>n.nodeType===Node.TEXT_NODE).map(n=>n.textContent||'')
      .join('').replace(/\s+/g,' ').trim().toLowerCase()""")
    if own_text != 'selection':
        raise SelectionReadError('Selection dugmesi etiketi uyusmuyor; '
                                 +str(_clinical_page_probe(page)))
    verify_clinical_url(page.url,sample)
    if stop and stop():raise SelectionReadError('Tarama durduruldu')
    try:
        button.click(timeout=10000)
    except Exception as exc:
        raise SelectionReadError('Selection dugmesine tiklanamadi; '
                                 +str(_clinical_page_probe(page))) from exc
    modal=page.locator(REPORT_MODAL)
    try:
        try:
            modal.wait_for(state='visible',timeout=timeout_ms)
        except Exception as exc:
            raise SelectionReadError('Clinical SNVs Selection penceresi acilmadi; sonuc kaydedilmedi') from exc
        # Count and row stability, plus strict validator. A half-loaded modal is NEVER a negative.
        last=None; stable=0; error=''
        for _ in range(30):
            if stop and stop():raise SelectionReadError('Tarama durduruldu')
            payload=modal.evaluate(r'''m => {
              const text=e=>(e?.textContent||'').replace(/\s+/g,' ').trim();
              const sel=(root,frag)=>root.querySelector('[class*="ReportSelection-module__'+frag+'__"]');
              const groups=[...m.querySelectorAll('[class*="ReportSelection-module__group__"]')];
              const rows=[];
              for (const g of groups){
                const group=text(sel(g,'groupTitle')).replace(/\d+\s*\/\s*\d+\s*$/,'').trim();
                for (const r of g.querySelectorAll('[class*="ReportSelection-module__optionRow__"]')){
                  // GenNext changes accessible label according to inclusion state:
                  // Included: Exclude variant in report (checked)
                  // Candidate: Include variant in report (unchecked).
                  const cbs=[...r.querySelectorAll('input[type="checkbox"]')].filter(
                    x=>['Include variant in report','Exclude variant in report'].includes(x.getAttribute('aria-label')||''));
                  const cb=cbs.length===1?cbs[0]:null;
                  const lbl=cb?.getAttribute('aria-label')||'';
                  const checked=cb && ((lbl==='Exclude variant in report' && cb.checked===true) ||
                    (lbl==='Include variant in report' && cb.checked===false)) ? cb.checked : null;
                  const meta=sel(r,'meta'); const metaText=text(meta);
                  const exon=(metaText.match(/\bExon\s+(\d+(?:\s*\/\s*\d+)?)/i)||[])[1]||'';
                  const cdna=(metaText.match(/\bc\.[-+*0-9_]+[ACGTNacgtn]+\s*>\s*[ACGTNacgtn-]+/)||[])[0]||'';
                  const genomic=(metaText.match(/\bchr[\w]+:\d+\s+[ACGTN-]+\s*>\s*[ACGTN-]+/i)||[])[0]||'';
                  const ad_dp=(metaText.match(/\bAD\s*\/\s*DP\s+(\d+\s*\/\s*\d+)/i)||[])[1]||'';
                  rows.push({checked,group,gene:text(sel(r,'gene')),
                    protein:text(sel(r,'hgvs')),consequence:text(sel(r,'consequence')),
                    cdna,genomic,exon,ad_dp,
                    vaf:text(sel(r,'vafValue')),tier:text(sel(r,'tierSlot')),
                    filter:text(sel(r,'statusSlot')),origin:text(sel(r,'workgroupSource')||sel(r,'privateSource')||sel(r,'selectionSource'))});
                }
              }
              // Real GenNext (2026-10): emptyState__... + the exact heading
              // "No variants selected" + non-editing footer "Continue with no variants".
              // Do not interpret a blank/unhydrated table as an empty result.
              const empty=m.querySelector('[class*="ReportSelection-module__emptyState__"]');
              const emptyHeading=text(empty?.querySelector('h5')).toLowerCase()==='no variants selected';
              const emptyContinue=[...m.querySelectorAll('.modal-footer button')].some(
                  b=>text(b).toLowerCase()==='continue with no variants');
              return {modal_present:!!m,header:text(sel(m,'includedCount')),
                groups:groups.map(g=>({count:text(sel(g,'count')),name:text(sel(g,'groupTitle'))})),
                rows,explicit_empty:!!(empty && emptyHeading && emptyContinue)};
            }''')
            signature=json.dumps(payload,sort_keys=True,ensure_ascii=False)
            try:
                parsed=parse_selection_data(payload)
                stable=(stable+1) if signature==last else 1
                # Empty candidate lists can briefly appear during React loading;
                # require >3 s of unchanged modal evidence before archiving 0.
                required=12 if payload.get('explicit_empty') else 3
                if stable>=required:return parsed
            except SelectionReadError as e:
                error=str(e);stable=0
            last=signature
            page.wait_for_timeout(300)
        raise SelectionReadError('Selection eksiksiz ve kararlı okunamadı: '+error)
    finally:
        # Only dismiss the UI; no checkboxes, report, or persistent platform data changed.
        try:
            close=modal.locator('button[aria-label="Close report selection"], button[aria-label="Close"]')
            if close.count():close.first.click(timeout=1500)
        except Exception:pass
