"""Per-export masked-source copies. Never modify the sequencer's original data.

Only source-data duplicates in the outgoing AI ZIP are sanitized. Scientific
coordinates, read counts, VAF, filters, QC metrics and other molecular columns
are preserved. Direct identifying names, Turkish national IDs and emails are
redacted. Unsupported/binary ZIP members are NOT forwarded unexamined.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import zipfile

from export_privacy_v0482 import redact_email_addresses

_ALLOWED_TEXT={'.tsv','.csv','.txt','.json','.xml','.html','.htm','.yaml','.yml','.log','.md'}
_MAX_FILE=90*1024*1024
_MAX_ARCHIVE_TOTAL=200*1024*1024
_ID_CANDIDATES=re.compile(r'(?<!\d)\d{11}(?!\d)')
_PRIVATE_FIELDS={'patientname','patient_name','fullname','full_name','firstname','first_name',
    'lastname','last_name','nationalid','national_id','tc','tckn','tcno','tc_no','tckimlik',
    'tc_kimlik','tckimlikno','identitynumber','identity_number',
    'requesting_physician','reporting_physician','patient_ad_soyad','hastaadi','hastasoyadi'}


def valid_tckn(value):
    """Checks standard 11-digit checksum without treating genomic coordinates as IDs."""
    val=str(value or '')
    if len(val)!=11 or not val.isdigit() or val[0]=='0':
        return False
    d=[int(c) for c in val]
    return ((7*sum(d[i] for i in (0,2,4,6,8))-sum(d[i] for i in (1,3,5,7)))%10==d[9]
            and sum(d[:10])%10==d[10])


def _key(text):
    return re.sub(r'[^a-z0-9_]', '', str(text or '').lower().strip().replace(' ','_'))


class Masker:
    def __init__(self,names=(),national_ids=()):
        self.names=[str(x).strip() for x in names if str(x).strip()]
        self.known_ids=set(str(x).strip() for x in national_ids if str(x).strip())
        self.name_re=(re.compile('|'.join(re.escape(x) for x in sorted(set(self.names),key=len,reverse=True)),re.I)
                      if self.names else None)
        self.hidden_names=0
        self.hidden_ids=0
        self.hidden_emails=0

    def text(self,text):
        text=str(text or '')
        if self.name_re:
            text,n=self.name_re.subn('[AD GIZLENDI]',text)
            self.hidden_names+=n
        def hide_id(match):
            number=match.group(0)
            if number in self.known_ids or valid_tckn(number):
                self.hidden_ids+=1
                return '[TC GIZLENDI]'
            return number
        text=_ID_CANDIDATES.sub(hide_id,text)
        new=redact_email_addresses(text)
        if new != text:
            self.hidden_emails+=1
        return new

    def name(self,name):
        return self.text(name).replace('\\','_').replace('/','_')

    def field(self,key,value):
        if _key(key) in _PRIVATE_FIELDS and value not in (None,'',[]):
            if _key(key) in {'nationalid','national_id','tc','tckn','tcno','tc_no','tckimlik','tc_kimlik','tckimlikno','identitynumber','identity_number'}:
                self.hidden_ids+=1
                return '[TC GIZLENDI]'
            self.hidden_names+=1
            return '[AD GIZLENDI]'
        if isinstance(value,str):
            return self.text(value)
        return value


def _transform_json(value,masker,parent=''):
    if isinstance(value,dict):
        cleaned={}
        for k,v in value.items():
            key=_key(k)
            if key=='name' and parent in ('patient','person','user','account','physician','analyst'):
                cleaned[masker.name(str(k))]='[AD GIZLENDI]'
                masker.hidden_names+=1
            else:
                cleaned[masker.name(str(k))]=_transform_json(masker.field(k,v),masker,key)
        return cleaned
    if isinstance(value,list):
        return [_transform_json(x,masker,parent) for x in value]
    if isinstance(value,str):
        return masker.text(value)
    return value


def _mask_delimited(text,masker,suffix):
    dialect='\t' if suffix=='.tsv' else ','
    reader=csv.reader(io.StringIO(text,newline=''),delimiter=dialect)
    output=io.StringIO(newline='')
    writer=csv.writer(output,delimiter=dialect,lineterminator='\n')
    header=None
    for line_index,row in enumerate(reader):
        if line_index==0:
            header=[_key(x) for x in row]
            writer.writerow([masker.text(x) for x in row])
            continue
        writer.writerow([masker.field(header[i],x) if header and i<len(header) else masker.text(x)
                         for i,x in enumerate(row)])
    return output.getvalue()


def _sanitize_text(raw,suffix,masker):
    data=raw.decode('utf-8-sig',errors='replace')
    if suffix=='.json':
        try:
            parsed=json.loads(data)
        except (ValueError,TypeError,RecursionError):
            return None
        return json.dumps(_transform_json(parsed,masker),ensure_ascii=False,separators=(',',':')).encode('utf-8')
    if suffix in ('.tsv','.csv'):
        return _mask_delimited(data,masker,suffix).encode('utf-8')
    return masker.text(data).encode('utf-8')


def _safe_zip_name(name,masker):
    pos=PurePosixPath(name.replace('\\','/'))
    if pos.is_absolute() or '..' in pos.parts:
        return None
    parts=[masker.name(x) for x in pos.parts if x not in ('','.','/')]
    return '/'.join(parts) if parts else None


def _sanitize_zip(src,dst,masker):
    removed=[]
    written=0
    with zipfile.ZipFile(src,'r') as zin, zipfile.ZipFile(dst,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as zout:
        total=0
        for info in zin.infolist():
            if info.is_dir(): continue
            total+=info.file_size
            suffix=PurePosixPath(info.filename).suffix.lower()
            safe=_safe_zip_name(info.filename,masker)
            if (not safe or suffix not in _ALLOWED_TEXT or info.file_size>_MAX_FILE
                or total>_MAX_ARCHIVE_TOTAL):
                removed.append({'type':'unsupported_or_oversize','extension':suffix})
                continue
            payload=_sanitize_text(zin.read(info),suffix,masker)
            if payload is None:
                removed.append({'type':'unreadable_structured_source','extension':suffix})
                continue
            zout.writestr(safe,payload)
            written+=1
    if not written:
        dst.unlink(missing_ok=True)
        return None, removed
    return dst, removed


def sanitize_source(src, arc, masker, staging_dir):
    """Return (sanitized source, sanitized bundle path, status) or None.

    Every outgoing copy is held only in ephemeral staging until ZIP publishing.
    No source changes are made in-place. Unsupported formats are excluded.
    """
    src=Path(src)
    suffix=src.suffix.lower()
    if not src.is_file() or suffix not in _ALLOWED_TEXT|{'.zip'}:
        return None
    if src.stat().st_size>_MAX_ARCHIVE_TOTAL:
        return None
    digest=hashlib.sha256((str(src.resolve())+str(src.stat().st_mtime_ns)).encode()).hexdigest()[:18]
    dst=Path(staging_dir)/(digest+suffix)
    try:
        if suffix=='.zip':
            saved,removed=_sanitize_zip(src,dst,masker)
            if saved is None:
                return None
        else:
            raw=src.read_bytes()
            if len(raw)>_MAX_FILE:return None
            payload=_sanitize_text(raw,suffix,masker)
            if payload is None:return None
            dst.write_bytes(payload)
            removed=[]
        arc_parts=PurePosixPath(str(arc).replace('\\','/')).parts
        cleaned='/'.join(masker.name(p) for p in arc_parts)
        if not cleaned.startswith('SOURCE_FILES/') or '..' in PurePosixPath(cleaned).parts:
            dst.unlink(missing_ok=True)
            return None
        return dst,cleaned,{'source_redacted':True,'omitted_binary_or_unsupported_members':len(removed),
                    'source_sha256_before_redaction':hashlib.sha256(src.read_bytes()).hexdigest(),
                    'source_sha256_after_redaction':hashlib.sha256(dst.read_bytes()).hexdigest(),
                    'redacted_bytes':dst.stat().st_size}
    except (OSError,ValueError,RuntimeError,zipfile.BadZipFile,UnicodeError,csv.Error):
        dst.unlink(missing_ok=True)
        return None


def sanitized_source_has_direct_identifiers(path,masker):
    """Fail closed if a completed redacted artifact retains recognized identifiers."""
    try:
        p=Path(path)
        def findings(text):
            if masker.name_re and masker.name_re.search(text):return True
            if any(x in text for x in masker.known_ids):return True
            if any(valid_tckn(x.group(0)) for x in _ID_CANDIDATES.finditer(text)):return True
            return '@' in text and bool(re.search(r'\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}',text))
        if p.suffix.lower()=='.zip':
            with zipfile.ZipFile(p,'r') as z:
                return any(findings(info.filename) or findings(z.read(info).decode('utf-8',errors='replace'))
                           for info in z.infolist() if not info.is_dir())
        return findings(p.name) or findings(p.read_text(encoding='utf-8',errors='replace'))
    except (OSError,zipfile.BadZipFile,UnicodeError,ValueError):
        return True
