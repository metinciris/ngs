# -*- coding: utf-8 -*-
"""Read-only extraction of the currently DISPLAYED ENLIL pathology report.

No ENLIL navigation, focus changes, input events, clipboard access or report edits.
Only the existing JAB worker may call these methods. The retrieved report remains
in the local UI until the user confirms saving a separate local copy.
"""
from __future__ import annotations

import hashlib
import re
from enlil_lookup import normalize_biopsy_query, biopsy_key

MAX_REPORT_CHARS = 60000
MAX_CANDIDATES = 20
CHUNK_CHARS = 700  # JAB getAccessibleTextRange has a short buffer size.
TEXT_ROLES = ('text', 'text area', 'editor pane', 'paragraph', 'document')


def _norm(value):
    return str(value or '').strip()


def _info(node):
    return getattr(node, 'context_info', None)


def _accessible_length(node):
    info = _info(node)
    if info is None or not getattr(info, 'accessibleText', False):
        return 0
    parser = getattr(node, 'text', None)
    meta = getattr(parser, 'info', None)
    try:
        return int(getattr(meta, 'charCount', 0) or 0)
    except (ValueError, TypeError):
        return 0


def _node_content(worker, node, *, limit=MAX_REPORT_CHARS):
    """Never request text from nodes without accessibleText unless it is a short field.

    Read exact character intervals in bounded chunks; no getTextContents writes.
    """
    count = _accessible_length(node)
    if count:
        if count > limit:
            return ''  # truncated reports are NOT saved as complete
        output = []
        for start in range(0, count, CHUNK_CHARS):
            size = min(CHUNK_CHARS, count - start)
            # JAB's range uses inclusive end indices; length includes NUL.
            chunk = worker.jab.get_accessible_text_range(node.context, start, start + size - 1, size + 1)
            if not isinstance(chunk, str) or not chunk:
                return ''
            # Java AccessibleText indices are UTF-16 code units.  A short
            # return must never be mistaken for a complete signed report.
            # Python's code-point count differs for emoji/supplementary chars;
            # compare UTF-16 units instead of accepting a truncated margin.
            if len(chunk.encode('utf-16-le', errors='surrogatepass')) // 2 != size:
                return ''
            output.append(chunk)
        content = ''.join(output)
        if len(content.encode('utf-16-le', errors='surrogatepass')) // 2 != count:
            return ''
        return content
    return ''


def _read_short_field(worker, node):
    content = _node_content(worker, node, limit=140)
    if content:
        return _norm(content)
    val = getattr(getattr(node, 'value', None), 'current', '')
    if val:
        return _norm(val)
    return worker.node_name(node)


def _visible_candidate(worker, node):
    role = worker.node_role(node)
    if role not in TEXT_ROLES:
        return False
    info = _info(node)
    states = worker.node_states(node)
    if any(marker in states for marker in ('invisible', 'hidden', 'offscreen')):
        return False
    width = int(getattr(info, 'width', 0) or 0)
    height = int(getattr(info, 'height', 0) or 0)
    # The RTF editor is far larger than a one-line patient/search field.
    return width >= 300 and height >= 95 and 150 <= _accessible_length(node) <= MAX_REPORT_CHARS


def _molecular_score(text):
    low = text.casefold()
    score = 0
    for marker in ('mutasyon', 'saptanmıştır', 'saptanmamıştır', 'füzyon',
                   'mikrosatellit', 'msi', 'tmb', 'hrd', 'cnv', 'kopya sayısı',
                   'moleküler', 'nükleotit', 'nukleotit', 'ekzon', 'geninde'):
        if marker in low:
            score += 1
    # At least 2 independent indicators to avoid taking patient-details pane.
    return score


def _status_seen(worker, nodes):
    # Hidden/stale Java controls are not proof that the opened report is final.
    for n in nodes:
        states = worker.node_states(n)
        if any(marker in states for marker in ('invisible', 'hidden', 'offscreen')):
            continue
        name = worker.node_name(n).casefold()
        if 'kesinleşmiş' in name or 'kesinlesmis' in name:
            return True
    return False


def _report_id(nodes, worker):
    """Advisory only: visible report ID is not an e-signature confirmation."""
    for n in nodes:
        if worker.node_name(n).casefold().replace('ı', 'i') not in ('rapor id', 'rapor id:'):
            continue
        parent = getattr(n, 'parent', None)
        if parent is None:
            continue
        ch = list(getattr(parent, 'children', []) or [])
        try:idx = ch.index(n)
        except ValueError:continue
        for next_node in ch[idx + 1:idx + 4]:
            match = re.fullmatch(r'\d{4,12}', _norm(worker.node_name(next_node)))
            if match:return match.group()
    return ''


def capture_displayed_report(worker, expected_mp):
    """Return a local-only payload or raise ValueError with no clinical text.

    Preconditions: ENLIL Patoloji/Raporlama is open, the *intended* patient's
    pathology row is loaded, the Tanı/Yorum tab is selected, and the report is
    visibly finalized. Users preview/confirm the saved copy in a separate step.
    """
    expected = normalize_biopsy_query(expected_mp)
    worker.ensure_connected()
    worker.refresh_accessible_context()
    tree = worker.build_tree()  # exactly one snapshot: no repeated background scan
    nodes = list(tree)
    checkbox = worker.find_checkbox(tree, 'Biopsi No')
    if checkbox is None:
        raise ValueError('Enlil Raporlama/Biyopsi No filtresi görünmüyor; rapor açılmadı.')
    field = worker.sibling_text_field(checkbox)
    if field is None:
        raise ValueError('Enlil Biyopsi No alanı okunamıyor; rapor çekilmedi.')
    visible_mp = biopsy_key(_read_short_field(worker, field))
    if not visible_mp or visible_mp != expected:
        raise ValueError('Enlil ekranındaki Biyopsi No istenen MP ile eşleşmiyor; başka hastanın raporu alınmadı.')
    # The biopsy filter ALONE does not prove the result is current. Confirm the
    # post-search pathology table row independently (stale-view protection).
    rows = worker.extract_pathology_rows(tree, expected_query=expected)
    matching = [r for r in rows if biopsy_key(r.get('biyopsi_no')) == expected]
    if len(matching) != 1:
        raise ValueError('Enlil sonuç tablosunda MP satırı tekil olarak doğrulanamadı; rapor çekilmedi.')
    row = dict(matching[0])
    if not _status_seen(worker, nodes):
        raise ValueError('Ekranda Kesinleşmiş durumu doğrulanamadı; tamamlanmamış rapor çekilmedi.')
    tabs = [n for n in nodes if worker.node_role(n) in ('page tab', 'tab') and
            'tanı/yorum' in worker.node_name(n).casefold()]
    if tabs and not any('selected' in worker.node_states(n) for n in tabs):
        raise ValueError('Tanı/Yorum sekmesi seçili değil; görüntülenen başka bölüm alınmadı.')
    candidates = []
    for n in nodes:
        if not _visible_candidate(worker, n):
            continue
        try:text = _node_content(worker, n)
        except Exception:continue
        if not text or len(text.strip()) < 150:
            continue
        score = _molecular_score(text)
        if score < 2:continue
        candidates.append((score, len(text), text.strip()))
        if len(candidates) >= MAX_CANDIDATES:
            raise ValueError('Enlil erişilebilirlik ağacında çok sayıda olası rapor metni var; otomatik seçim güvenli değil.')
    if not candidates:
        raise ValueError('Tanı/Yorum içindeki NGS metni Java erişilebilirlik katmanından okunamadı; kaydedilmedi. Yapıştırma seçeneği kullanılabilir.')
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    best_score, _, best_text = candidates[0]
    if len(candidates) > 1:
        other = candidates[1]
        if (other[0] >= best_score and
                hashlib.sha256(other[2].encode('utf-8')).digest() != hashlib.sha256(best_text.encode('utf-8')).digest()):
            raise ValueError('Enlil ekranında birden fazla eşit olasılıklı rapor metni var; otomatik seçim yapılmadı.')
    return {'query': expected, 'observed_biopsy': visible_mp,
            'report_id_on_screen': _report_id(nodes, worker),
            'screen_finalized_label_seen': True, 'enlil_table_identity': {
                'biyopsi_no': visible_mp,
                'dosya_no': _norm(row.get('dosya_no')),
                'tc_no': _norm(row.get('tc_no')),
                'dogum_tarihi': _norm(row.get('dogum_tarihi')),
            },
            'text': best_text,
            'sha256': hashlib.sha256(best_text.encode('utf-8')).hexdigest(),
            'source': 'enlil_open_screen_jab_read_only'}
