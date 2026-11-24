"""Explicit user-confirmed cross-MP DNA/RNA links; no name/block-only auto matching.

Stored locally under DATA, not in NGS exports.  Merely viewing is read-only.
"""
from __future__ import annotations
import json
from pathlib import Path
from datetime import datetime
from patient_workspace_model import case_key


def _path(base):
    return Path(base) / 'DATA' / 'ASSAY_LINKS' / 'dna_rna_pairs.json'


def _read(base, strict=False):
    path=_path(base)
    if not path.exists():return []
    try:
        raw=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(raw,dict) or raw.get('schema')!='ngs_user_confirmed_dna_rna_links_v1' or not isinstance(raw.get('pairs'),list):
            raise ValueError('Invalid pair link data')
        if any(not isinstance(p,dict) or not all(isinstance(p.get(k),str) for k in ('a','b')) for p in raw['pairs']):
            raise ValueError('Invalid link entry')
        return raw['pairs']
    except (OSError,UnicodeError,ValueError,TypeError) as exc:
        if strict:raise ValueError('Eşleştirme dosyası okunamıyor; üzerine yazılmadı.') from exc
        # Do not incorrectly link anything when source is damaged.
        return []


def _checks(db,a,b):
    if not a or not b or a==b:raise ValueError('İki ayrı teknik örnek seçilmeli.')
    ra,rb=db.get_sample(a),db.get_sample(b)
    if not ra or not rb:raise ValueError('Yerel teknik kayıt bulunamadı.')
    if {str(ra['sample_type']).upper(),str(rb['sample_type']).upper()}!={'DNA','RNA'}:
        raise ValueError('Eşleştirme yalnız bir DNA ve bir RNA teknik kaydı arasında yapılır.')
    cma=db.get_case_metadata(ra['case_family'] or ra['patient_id'] or '')
    cmb=db.get_case_metadata(rb['case_family'] or rb['patient_id'] or '')
    ida=''.join(ch for ch in str(cma.get('national_id') or '') if ch.isascii() and ch.isdigit())
    idb=''.join(ch for ch in str(cmb.get('national_id') or '') if ch.isascii() and ch.isdigit())
    if db.validate_national_id(ida) and db.validate_national_id(idb) and ida!=idb:
        raise ValueError('Doğrulanmış TC kimlik numaraları farklı; bağlantı engellendi.')
    if cma.get('birth_date') and cmb.get('birth_date') and cma['birth_date']!=cmb['birth_date']:
        raise ValueError('Doğum tarihleri farklı; kimlik doğrulanmadan bağlantı kurulamaz.')
    if cma.get('sex') and cmb.get('sex') and db.normalize_sex(cma['sex'])!=db.normalize_sex(cmb['sex']):
        raise ValueError('Cinsiyet bilgileri uyuşmuyor; bağlantı kurulamaz.')
    ma,mb=db.get_sample_metadata(a),db.get_sample_metadata(b)
    ida,idb=str(ma.get('block_id') or '').strip().casefold(),str(mb.get('block_id') or '').strip().casefold()
    if not ida or not idb or ida!=idb:
        raise ValueError('Blok numaraları her iki teknik kayıtta aynı ve dolu olmalı; elle onay için önce blokları doğrulayın.')
    sa,sb=str(ma.get('block_source') or '').strip().casefold(),str(mb.get('block_source') or '').strip().casefold()
    if sa and sb and sa!=sb:
        raise ValueError('Blok kaynağı farklı; otomatik eşleştirme yapılmadı.')
    return dict(ra),dict(rb)


def manual_related_records(base,db,record_keys):
    """Only explicit direct links for source records. No transitive chain linking."""
    keys=set(record_keys)
    result=[];seen=set(keys)
    for pair in _read(base):
        a,b=pair['a'],pair['b']
        target=b if a in keys else a if b in keys else ''
        if not target or target in seen:continue
        try:_checks(db,a,b)
        except ValueError:continue
        row=db.get_sample(target)
        if row:
            d=dict(row);d['_user_confirmed_link']=True
            result.append(d);seen.add(target)
    return result


def linked_pair(base,a,b):
    target=set((a,b))
    return any(set((p['a'],p['b']))==target for p in _read(base))


def confirm_pair(base,db,a,b):
    ra,rb=_checks(db,a,b)
    if case_key(ra)==case_key(rb):raise ValueError('Aynı MP ailesi zaten birlikte listeleniyor; manuel eşleştirme gerekmiyor.')
    pairs=_read(base,strict=True)
    target=set((a,b))
    if any(set((p['a'],p['b']))==target for p in pairs):return False
    pair={'a':a,'b':b,'confirmed_at':datetime.now().astimezone().isoformat(timespec='seconds')}
    pairs.append(pair)
    _write(base,pairs)
    return True


def remove_pair(base,a,b):
    pairs=_read(base,strict=True)
    new=[p for p in pairs if set((p['a'],p['b']))!=set((a,b))]
    if len(new)==len(pairs):return False
    _write(base,new)
    return True


def _write(base,pairs):
    path=_path(base);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps({'schema':'ngs_user_confirmed_dna_rna_links_v1','pairs':pairs},ensure_ascii=False,indent=2),encoding='utf-8')
    tmp.replace(path)
