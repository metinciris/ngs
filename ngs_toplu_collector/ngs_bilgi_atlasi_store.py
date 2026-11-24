# -*- coding: utf-8 -*-
"""NGS Bilgi Atlasi - independent local knowledge store (stdlib only).

No reads/writes against collector patient databases. No network requests.
The atlas stores research/technical *notes*, not validated clinical observations.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import shutil
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
CATEGORIES = (
    'Teknik hafıza', 'Raporlama sözlüğü', 'Veri kaynakları',
    'Bilimsel bilgi', 'Çalışma notu', 'Kod haritası', 'Diğer',
)
SOURCE_KINDS = ('Kod', 'Web/PubMed', 'Enlil', 'GenNext', 'MultiQC', 'Manuel', 'Yapay zekâ', 'Diğer')
STATUSES = ('Doğrulama bekliyor', 'Yol gösterici', 'Doğrulandı', 'Gözden geçirilmeli', 'Geçersiz/eski', 'Otomatik indeks')


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def search_normalize(value: str) -> str:
    text = str(value or '').casefold().replace('ı', 'i').replace('İ', 'i')
    text = unicodedata.normalize('NFKD', text)
    return ''.join(ch for ch in text if not unicodedata.combining(ch))


def safe_text(value, limit: int) -> str:
    val = str(value or '').replace('\x00', '').strip()
    if len(val) > limit:
        raise ValueError(f'Metin cok uzun (en fazla {limit} karakter).')
    return val


def stable_code_id(path: str, qualified: str) -> str:
    return 'code:' + hashlib.sha256((path + '|' + qualified).encode('utf-8')).hexdigest()[:32]


class AtlasStore:
    def __init__(self, base_dir: str | Path):
        self.root = Path(base_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / 'atlas.sqlite3'
        self._initialize()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys = ON')
        conn.execute('PRAGMA busy_timeout = 15000')
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self):
        with self._connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS atlas_meta (
                    name TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS entries (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    category TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    source_label TEXT NOT NULL DEFAULT '',
                    source_uri TEXT NOT NULL DEFAULT '',
                    acquisition TEXT NOT NULL DEFAULT '',
                    source_date TEXT NOT NULL DEFAULT '',
                    source_version TEXT NOT NULL DEFAULT '',
                    author TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    details TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'Doğrulama bekliyor',
                    tags TEXT NOT NULL DEFAULT '',
                    linked_module TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    generated INTEGER NOT NULL DEFAULT 0,
                    search_text TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_atlas_entries_kind ON entries(source_kind);
                CREATE INDEX IF NOT EXISTS idx_atlas_entries_category ON entries(category);
                CREATE INDEX IF NOT EXISTS idx_atlas_entries_modified ON entries(generated,updated_at);
                CREATE TABLE IF NOT EXISTS entry_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entry_id TEXT NOT NULL REFERENCES entries(id),
                    revision INTEGER NOT NULL,
                    event TEXT NOT NULL,
                    snapshot TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    edited_at TEXT NOT NULL,
                    actor TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS notes (
                    id TEXT PRIMARY KEY,
                    entry_id TEXT NOT NULL REFERENCES entries(id),
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source_uri TEXT NOT NULL DEFAULT '',
                    acquisition TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    author TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_atlas_notes_entry ON notes(entry_id);
                CREATE TABLE IF NOT EXISTS extra_sources (
                    id TEXT PRIMARY KEY,
                    entry_id TEXT NOT NULL REFERENCES entries(id),
                    source_kind TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    uri TEXT NOT NULL DEFAULT '',
                    acquisition TEXT NOT NULL DEFAULT '',
                    observed_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'Doğrulama bekliyor'
                );
                CREATE TABLE IF NOT EXISTS code_files (
                    path TEXT PRIMARY KEY,
                    sha256 TEXT NOT NULL,
                    indexed_at TEXT NOT NULL,
                    symbol_count INTEGER NOT NULL DEFAULT 0
                );
            ''')
            db.execute('INSERT OR IGNORE INTO atlas_meta(name,value) VALUES (?,?)',
                       ('schema_version', str(SCHEMA_VERSION)))

    @staticmethod
    def _fields(entry):
        fields = {
            'title': safe_text(entry.get('title'), 250),
            'category': safe_text(entry.get('category') or 'Çalışma notu', 80),
            'source_kind': safe_text(entry.get('source_kind') or 'Manuel', 80),
            'source_label': safe_text(entry.get('source_label'), 500),
            'source_uri': safe_text(entry.get('source_uri'), 2000),
            'acquisition': safe_text(entry.get('acquisition'), 3000),
            'source_date': safe_text(entry.get('source_date'), 100),
            'source_version': safe_text(entry.get('source_version'), 200),
            'author': safe_text(entry.get('author'), 160),
            'summary': safe_text(entry.get('summary'), 4000),
            'details': safe_text(entry.get('details'), 120000),
            'status': safe_text(entry.get('status') or 'Doğrulama bekliyor', 100),
            'tags': safe_text(entry.get('tags'), 1000),
            'linked_module': safe_text(entry.get('linked_module'), 500),
        }
        if not fields['title']:
            raise ValueError('Baslik bos olamaz.')
        if fields['category'] not in CATEGORIES or fields['source_kind'] not in SOURCE_KINDS:
            raise ValueError('Gecersiz kategori veya kaynak turu.')
        if fields['status'] not in STATUSES:
            raise ValueError('Gecersiz dogrulama durumu.')
        if fields['source_kind'] != 'Kod' and not fields['acquisition']:
            raise ValueError('Bilginin nasil elde edildigini yazin.')
        return fields

    @staticmethod
    def _search_text(fields):
        return search_normalize(' '.join(str(fields.get(k, '')) for k in
            ('title','category','source_kind','source_label','source_uri',
             'acquisition','source_version','summary','details','tags','linked_module')))

    def save_entry(self, entry: dict, reason: str = '', actor: str = 'Kullanici') -> str:
        fields = self._fields(entry)
        if fields['status'] == 'Otomatik indeks':
            raise ValueError('Otomatik indeks durumu yalnizca kod taramasinda kullanilir.')
        rid = entry.get('id') or 'note:' + uuid.uuid4().hex
        now = timestamp()
        with self._connect() as db:
            before = db.execute('SELECT * FROM entries WHERE id=?', (rid,)).fetchone()
            if before and before['generated']:
                raise ValueError('Kod haritası kayitlari degistirilemez. Bunun yerine not ekleyin.')
            revision = before['revision'] + 1 if before else 1
            generated = 0
            record = dict(fields, id=rid, created_at=before['created_at'] if before else now,
                          updated_at=now, revision=revision, generated=generated,
                          search_text=self._search_text(fields))
            columns = list(record)
            db.execute('INSERT INTO entries(' + ','.join(columns) + ') VALUES (' +
                       ','.join('?' for _ in columns) + ') ON CONFLICT(id) DO UPDATE SET ' +
                       ','.join(f'{x}=excluded.{x}' for x in columns if x not in ('id','created_at')),
                       [record[c] for c in columns])
            db.execute('INSERT INTO entry_revisions(entry_id,revision,event,snapshot,reason,edited_at,actor) '
                       'VALUES (?,?,?,?,?,?,?)',
                       (rid, revision, 'updated' if before else 'created',
                        json.dumps(record,ensure_ascii=False),safe_text(reason,1000),now,safe_text(actor,160)))
        return rid

    def seed_entry(self, entry_id: str, **fields) -> bool:
        fields.setdefault('status', 'Gözden geçirilmeli')
        fields.setdefault('category', 'Teknik hafıza')
        fields.setdefault('source_kind', 'Kod')
        fields.setdefault('acquisition', 'Kaynak kodu ve tanisal kayitlardan elle derlendi; klinik dogrulama degildir.')
        validated = self._fields(fields)
        now = timestamp()
        record = dict(validated,id=entry_id,created_at=now,updated_at=now,revision=1,generated=0,
                      search_text=self._search_text(validated))
        with self._connect() as db:
            if db.execute('SELECT 1 FROM entries WHERE id=?',(entry_id,)).fetchone():
                return False
            cols = list(record)
            db.execute('INSERT INTO entries('+','.join(cols)+') VALUES ('+
                       ','.join('?' for _ in cols)+')',[record[c] for c in cols])
            db.execute('INSERT INTO entry_revisions(entry_id,revision,event,snapshot,reason,edited_at,actor) '
                       'VALUES (?,?,?,?,?,?,?)',
                       (entry_id,1,'seed',json.dumps(record,ensure_ascii=False),
                        'Ilk teknik harita; kullanici tarafindan dogrulanmadi',now,'Atlas'))
        return True

    def get_entry(self, record_id: str):
        with self._connect() as db:
            row = db.execute('SELECT * FROM entries WHERE id=?',(record_id,)).fetchone()
            return dict(row) if row else None

    def list_entries(self, query='', category='Tumu', source_kind='Tumu', limit=25, offset=0):
        clause = []
        args = []
        for term in search_normalize(query).split():
            clause.append('instr(search_text,?) > 0')
            args.append(term)
        if category not in ('', 'Tumu', 'Tümü'):
            clause.append('category=?'); args.append(category)
        if source_kind not in ('', 'Tumu', 'Tümü'):
            clause.append('source_kind=?'); args.append(source_kind)
        where = ' WHERE ' + ' AND '.join(clause) if clause else ''
        sql = ('SELECT id,title,category,source_kind,source_label,source_uri,acquisition,'
               'source_date,source_version,author,summary,details,status,tags,linked_module,'
               'created_at,updated_at,revision,generated FROM entries' + where +
               ' ORDER BY generated ASC, updated_at DESC, title COLLATE NOCASE LIMIT ? OFFSET ?')
        with self._connect() as db:
            total = db.execute('SELECT COUNT(*) FROM entries' + where,args).fetchone()[0]
            rows = [dict(x) for x in db.execute(sql, args+[min(150,max(1,limit)),max(0,offset)])]
        return rows,total

    def add_note(self, record_id: str, content: str, kind: str = 'Manuel',
                 source_uri: str = '', acquisition: str = '', author: str = ''):
        note = {'id':'sub:'+uuid.uuid4().hex,'entry_id':record_id,
                'kind':safe_text(kind,80),'content':safe_text(content,50000),
                'source_uri':safe_text(source_uri,2000),
                'acquisition':safe_text(acquisition,2000),
                'created_at':timestamp(), 'author':safe_text(author,160)}
        if not note['content'] or note['kind'] not in SOURCE_KINDS:
            raise ValueError('Not icerigi ve kaynak turu zorunludur.')
        if not note['acquisition']:
            raise ValueError('Notun nasil edinildigi yazilmali.')
        with self._connect() as db:
            if not db.execute('SELECT 1 FROM entries WHERE id=?',(record_id,)).fetchone():
                raise ValueError('Kayit bulunamadi.')
            db.execute('INSERT INTO notes('+','.join(note)+') VALUES ('+
                       ','.join('?' for _ in note)+')',list(note.values()))
        return note['id']

    def add_source(self, record_id: str, kind: str, label: str,
                   uri: str, acquisition: str, status: str = 'Doğrulama bekliyor'):
        if kind not in SOURCE_KINDS or status not in STATUSES:
            raise ValueError('Gecersiz kaynak turu/durumu.')
        if not label.strip() or not acquisition.strip():
            raise ValueError('Kaynak adi ve elde edilme sekli zorunludur.')
        item = ('src:'+uuid.uuid4().hex,record_id,kind,
                safe_text(label,500),safe_text(uri,2000),safe_text(acquisition,2000),timestamp(),status)
        with self._connect() as db:
            db.execute('INSERT INTO extra_sources '
                       '(id,entry_id,source_kind,label,uri,acquisition,observed_at,status) '
                       'VALUES (?,?,?,?,?,?,?,?)',item)
        return item[0]

    def get_notes(self, record_id: str):
        with self._connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM notes WHERE entry_id=? ORDER BY created_at DESC,id DESC',(record_id,))]

    def get_sources(self, record_id: str):
        with self._connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM extra_sources WHERE entry_id=? ORDER BY observed_at DESC',(record_id,))]

    def get_history(self, record_id: str):
        with self._connect() as db:
            return [dict(r) for r in db.execute('SELECT revision,event,reason,edited_at,actor FROM entry_revisions '
                                                'WHERE entry_id=? ORDER BY id DESC',(record_id,))]

    def counts(self):
        with self._connect() as db:
            total = db.execute('SELECT COUNT(*) FROM entries').fetchone()[0]
            code = db.execute('SELECT COUNT(*) FROM entries WHERE generated=1').fetchone()[0]
            manual = total - code
            notes = db.execute('SELECT COUNT(*) FROM notes').fetchone()[0]
            return {'total':total,'code':code,'curated':manual,'notes':notes}

    def backup(self, to_path: str | Path):
        dest = Path(to_path)
        dest.parent.mkdir(parents=True,exist_ok=True)
        if dest.resolve() == self.db_path.resolve():
            raise ValueError('Yedek hedefi atlasin kendi veritabani olamaz.')
        with self._connect() as db:
            with sqlite3.connect(str(dest)) as dest_db:
                db.backup(dest_db)
        return dest

    def export_markdown(self, to_path: str | Path, include_code=True):
        output = Path(to_path)
        output.parent.mkdir(parents=True,exist_ok=True)
        with self._connect() as db:
            rows = db.execute('SELECT * FROM entries '+
                ('' if include_code else 'WHERE generated=0 ')+
                'ORDER BY generated ASC,category,title').fetchall()
            lines = ['# NGS Bilgi Atlasi','',
                     '> Kaynakli not arsivi; klinik karar ve dogrulanmis hasta verisi degildir.','',
                     f'Disa aktarma zamani (UTC): {timestamp()}','']
            for row in rows:
                entry = dict(row)
                lines += [f'## {entry["title"]}','',
                          f'- Kimlik: `{entry["id"]}`',
                          f'- Kategori: {entry["category"]}',
                          f'- Kaynak turu: {entry["source_kind"]}',
                          f'- Kaynak: {entry["source_label"]}',
                          f'- Adres/dosya: {entry["source_uri"]}',
                          f'- Elde edilme: {entry["acquisition"]}',
                          f'- Kaynak tarihi: {entry["source_date"]}',
                          f'- Kaynak surumu: {entry["source_version"]}',
                          f'- Durum: {entry["status"]}',
                          f'- Son degisiklik: {entry["updated_at"]}',
                          f'- Etiketler: {entry["tags"]}', '',
                          entry['summary'],'',entry['details'],'']
                sources = db.execute('SELECT * FROM extra_sources WHERE entry_id=?',(entry['id'],)).fetchall()
                if sources:
                    lines += ['### Ek kaynaklar','']
                    for item in sources:
                        lines += [f'- {item["source_kind"]}: {item["label"]} — {item["uri"]} '
                                  f'(Yontem: {item["acquisition"]}; durum: {item["status"]})']
                    lines.append('')
                notes = db.execute('SELECT * FROM notes WHERE entry_id=? ORDER BY created_at',(entry['id'],)).fetchall()
                if notes:
                    lines += ['### Eklenen notlar','']
                    for note in notes:
                        lines += [f'**{note["kind"]} · {note["created_at"]} · {note["author"]}**', '',
                                  note['content'],'',f'Kaynak: {note["source_uri"]} · Yontem: {note["acquisition"]}', '']
                lines += ['---','']
        output.write_text('\n'.join(lines),encoding='utf-8')
        return len(rows)

    def export_json(self, to_path: str | Path, include_code=True):
        with self._connect() as db:
            where = '' if include_code else 'WHERE generated=0'
            rows = [dict(row) for row in db.execute('SELECT * FROM entries '+where+' ORDER BY title')]
            sources = [dict(row) for row in db.execute('SELECT * FROM extra_sources')]
            notes = [dict(row) for row in db.execute('SELECT * FROM notes')]
            history = [dict(row) for row in db.execute('SELECT * FROM entry_revisions')]
        if not include_code:
            allowed = {r['id'] for r in rows}
            sources = [r for r in sources if r['entry_id'] in allowed]
            notes = [r for r in notes if r['entry_id'] in allowed]
            history = [r for r in history if r['entry_id'] in allowed]
        payload={'format':'ngs_bilgi_atlasi_v1','exported_utc':timestamp(),'entries':rows,
                 'extra_sources':sources,'notes':notes,'revisions':history}
        Path(to_path).write_text(json.dumps(payload,indent=2,ensure_ascii=False),encoding='utf-8')
        return len(rows)

    def index_code(self, code_root: str | Path):
        """Index top-level *.py/*.pyw AST only, WITHOUT function bodies/data."""
        base = Path(code_root).resolve()
        indexed_files = 0
        indexed_symbols = 0
        skipped = 0
        errors = []
        for file in sorted(base.iterdir()):
            if not file.is_file() or file.suffix.lower() not in ('.py','.pyw'):
                continue
            if file.name.startswith('ngs_bilgi_atlasi_'):
                continue
            if file.stat().st_size > 2_000_000:
                skipped += 1
                continue
            rel = file.name
            raw = file.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            with self._connect() as db:
                cached = db.execute('SELECT sha256 FROM code_files WHERE path=?',(rel,)).fetchone()
            if cached and cached['sha256']==digest:
                skipped += 1
                continue
            try:
                source = raw.decode('utf-8-sig')
                tree = ast.parse(source,filename=rel)
            except (UnicodeError,SyntaxError) as ex:
                errors.append(f'{rel}: {type(ex).__name__}')
                continue
            now = timestamp()
            symbols = []
            def add_item(node, name, kind):
                line = getattr(node,'lineno',1)
                last = getattr(node,'end_lineno',line)
                doc = ast.get_docstring(node) or ''
                doc = re.sub(r'\s+',' ',doc).strip()[:700]
                head = name
                if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):
                    params=[]
                    for arg in (node.args.posonlyargs + node.args.args + node.args.kwonlyargs):
                        params.append(arg.arg)
                    if node.args.vararg: params.append('*'+node.args.vararg.arg)
                    if node.args.kwarg: params.append('**'+node.args.kwarg.arg)
                    head = name + '(' + ', '.join(params[:18]) + (', ...' if len(params)>18 else '') + ')'
                return {
                    'id': stable_code_id(rel,name),
                    'title':head, 'category':'Kod haritası','source_kind':'Kod',
                    'source_label':f'{rel}:{line}-{last}',
                    'source_uri':f'{rel}#L{line}-L{last}',
                    'acquisition':'Python AST ile otomatik kod sembolu indeksi. Fonksiyon govdesi ve hasta verisi alinmaz.',
                    'source_date':now,'source_version':digest[:16], 'author':'Atlas / AST',
                    'summary':doc or f'{kind} sembolu. Amaci ve etkisi kaynak kodunda dogrulanmalidir.',
                    'details':f'Tur: {kind}\nDosya: {rel}\nSatir: {line}-{last}\nAd: {name}\n'
                              'Bu otomatik kod haritasi islevin klinik veya teknik olarak dogrulandigi anlamina gelmez.',
                    'status':'Otomatik indeks','tags':f'{rel}, {kind}',
                    'linked_module':rel,'created_at':now,'updated_at':now,'revision':1,'generated':1
                }
            module_name = rel.rsplit('.',1)[0]
            symbols.append(add_item(tree, module_name, 'Modul'))
            def walk(body,prefix='',depth=0):
                if depth > 5: return
                for n in body:
                    if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
                        name = prefix+'.'+n.name if prefix else n.name
                        kind='Sinif' if isinstance(n,ast.ClassDef) else ('Asenkron fonksiyon' if isinstance(n,ast.AsyncFunctionDef) else 'Fonksiyon')
                        symbols.append(add_item(n, name, kind))
                        if isinstance(n,ast.ClassDef):
                            walk(n.body,name,depth+1)
            walk(tree.body)
            with self._connect() as db:
                prev = {row['id'] for row in db.execute('SELECT id FROM entries WHERE generated=1 AND linked_module=?',(rel,))}
                active = set()
                for item in symbols:
                    active.add(item['id'])
                    item['search_text']=self._search_text(item)
                    before = db.execute('SELECT revision,created_at,search_text,source_version FROM entries WHERE id=?',(item['id'],)).fetchone()
                    if before:
                        item['revision']=before['revision']+1 if before['source_version']!=digest[:16] else before['revision']
                        item['created_at']=before['created_at']
                    columns = list(item)
                    db.execute('INSERT INTO entries('+','.join(columns)+') VALUES ('+
                               ','.join('?' for _ in columns)+') ON CONFLICT(id) DO UPDATE SET '+
                               ','.join(c+'=excluded.'+c for c in columns if c not in ('id','created_at')),
                               [item[c] for c in columns])
                    if before is None or before['source_version'] != digest[:16]:
                        db.execute('INSERT INTO entry_revisions(entry_id,revision,event,snapshot,reason,edited_at,actor) '
                                   'VALUES (?,?,?,?,?,?,?)',
                                   (item['id'],item['revision'],
                                    'code_index_created' if before is None else 'source_file_changed',
                                    json.dumps(item,ensure_ascii=False),
                                    'AST kod indeksi; kaynak dosya SHA256 degisti' if before else 'Ilk AST kod indeksi',
                                    now,'Atlas/AST'))
                # Keep old symbols for traceability, but indicate that they are no longer present.
                for stale_id in prev-active:
                    db.execute('UPDATE entries SET status=?,summary=?,updated_at=? WHERE id=?',
                               ('Geçersiz/eski','Sembol son kod indeksinde bulunmadi; eski surum kaydi.',now,stale_id))
                db.execute('INSERT INTO code_files(path,sha256,indexed_at,symbol_count) VALUES (?,?,?,?) '
                           'ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256,indexed_at=excluded.indexed_at,'
                           'symbol_count=excluded.symbol_count', (rel,digest,now,len(symbols)))
            indexed_files += 1
            indexed_symbols += len(symbols)
        return {'indexed_files':indexed_files,'indexed_symbols':indexed_symbols,'unchanged_or_skipped':skipped,'errors':errors}


def seed_technical_memory(store: AtlasStore):
    """Small, explicit source-grounded guide; never overwrite user edits."""
    records = [
        dict(id='seed:atlas:purpose',title='Bilgi Atlasi nasil kullanilir?', category='Teknik hafıza',
             source_kind='Manuel',source_label='NGS Bilgi Atlasi v1 kilavuzu',
             acquisition='Kurulum sirasinda eklenen uygulama kilavuzu; klinik veri degildir.',
             status='Yol gösterici',tags='atlas, kilavuz, kullanim',
             summary='Teknik hafıza, web kaynakli notlar, Enlil aciklamalari, manuel kararlar ve YZ yorumlari tek yerde saklanir.',
             details='Yeni Bilgi dugmesiyle kayit olusturun. Her kayit icin KAYNAK, ELDE EDILME YONTEMI, TARIH ve DOGRULAMA DURUMU yazin. '
                     'Kod Haritasini Yenile dugmesi programin Python modullerini ve sembollerini AST uzerinden indeksler. '
                     'Ek Not ile ilk kaydi degistirmeden yeni bilgi eklenir. JSON/Markdown disa aktarim ve SQLite yedegi alinabilir. '
                     'Kisisel/hasta verisi veya sifreleri bu atlasa girmeyin. Web ya da Enlil otomatik baglanti bu surumde yoktur.'),
        dict(id='seed:qc:multiqc',title='QC verilerinin MultiQC ZIP kaynaklari',category='Veri kaynakları',
             source_kind='Kod',source_label='qc_enlil.py: parse_multiqc_json_zip / parse_multiqc_tsv_zip',
             source_uri='qc_enlil.py',linked_module='qc_enlil.py',
             acquisition='v0.4.95 kaynak kodunda JSON/TSV MultiQC ayrıştırıcı fonksiyonlari incelenerek derlendi.',
             status='Gözden geçirilmeli',tags='QC, MultiQC, DNA, RNA',
             summary='QC modulu MultiQC JSON ve TSV ZIP dosyalarini okuyabilir. Her sayisal alanin ornek ve analiz kimligi teyit edilmelidir.',
             details='Konum: qc_enlil.py icindeki parse_multiqc_json_zip(), parse_multiqc_tsv_zip(), parse_multiqc_zip(). '
                     'Coklu ornek iceren TSV dosyalarinda ilk satiri dogru ornek varsaymak guvenli degildir. '
                     'Bu atlas veri akisini belgelendirir; QC raporlama kodunu degistirmez.'),
        dict(id='seed:qc:mean',title='DNA ortalama derinlik: kaynak ve rapora aktarim engeli',category='Raporlama sözlüğü',
             source_kind='MultiQC',source_label='qc_enlil.py ve 23.11.2026 salt-okunur tanisal ozet',
             source_uri='qc_enlil.py',linked_module='qc_enlil.py',
             acquisition='Kod alanlari ve kullanicinin sagladigi toplu QC tanisal tarama sonuclari karsilastirildi.',
             status='Gözden geçirilmeli',tags='DNA, mean_coverage_x, ortalama derinlik, rapor kapisi',
             summary='Ortalama derinlik birkac kayit icin parse ediliyor olabilir; raporda yoksa ornek/protokol eslestirme kapisi kontrol edilmeli.',
             details='qc_enlil.py SampleMetrics.mean_coverage_x, median_coverage_x, parse_multiqc_* ve report_sentence() zincirini kontrol edin. '
                     '23.11.2026 genel taramasinda 147 MultiQC ZIP, 103 DNA olarak okunan kayit ve bunlarin 87 tanesinde sayisal '
                     'ortalama derinlik saptanmis; rapor kapisi bu 87 kaydi durdurmustur. 130 ZIP cok ornekli TSV icerir. '
                     'Bunlar hasta bazli dogrulama degildir. Okuma ciftinden (paired-end pairs) ortalama hedef kapsama derinligi hesaplanmaz.'),
        dict(id='seed:qc:report',title='DNA/RNA QC cumlelerinin uretildigi kod',category='Raporlama sözlüğü',
             source_kind='Kod',source_label='qc_enlil.py: SampleMetrics.report_sentence',
             source_uri='qc_enlil.py',linked_module='qc_enlil.py',
             acquisition='v0.4.95 kaynak dosyasinda SampleMetrics rapor cumlesi fonksiyonu ve NGSQCPanel sinifi belirlendi.',
             status='Gözden geçirilmeli',tags='DNA, RNA, QC, Boyama',
             summary='Boyama metnine gelecek DNA/RNA QC cumleleri QC modulunde olusturulur.',
             details='DNA icin paired-end okuma cifti, GC, temiz veri, duplikasyon, FREEMIX; RNA icin okuma cifti ve temiz veri '
                     'mevcut is akisi ornekleridir. Her olcum ayri teknik ornege bagli olmalidir. '
                     'Atlasin kurulumu report_sentence() fonksiyonuna, QC parserlara ve hasta raporlarina dokunmaz.'),
        dict(id='seed:report:context',title='Rapor alanlarinin kaynak gecmisi ve dogrulama',category='Veri kaynakları',
             source_kind='Kod',source_label='reporting_context.py',source_uri='reporting_context.py',linked_module='reporting_context.py',
             acquisition='Kaynak kodunun alan sabitleri, ensure_schema ve dogrulama kurallari incelendi.',
             status='Gözden geçirilmeli',tags='raporlama, kayit, provenance, Enlil',
             summary='Raporlama alanlari ve kaynak duzeltme gecmisi reporting_context.py moduluyle yonetilir.',
             details='FIELDS, CLINICAL_MAP, normalize_field, ensure_schema ve ContextConflict incelenebilir. '
                     'Atlas ayri veritabanina yazar; report_context klinik verilerini iceri almaz.'),
        dict(id='seed:enlil',title='Enlil kaynakli bilgiler: once yontem ve kapsam',category='Veri kaynakları',
             source_kind='Kod',source_label='enlil_lookup.py / enlil_report_reader.py',
             source_uri='enlil_lookup.py',linked_module='enlil_lookup.py',
             acquisition='Kaynak ZIP indeki Enlil adli iki Python modulunun varligi dogrulandi.',
             status='Gözden geçirilmeli',tags='Enlil, manuel, raporlama',
             summary='Enlil kaynakli is akisi bilgileri atlas notu olarak kaydedilebilir; otomatik hasta kaydi aktarimi yapilmaz.',
             details='Enlil kaynakli not girerken olcumun nasil getirildigini, program surumunu ve orijinal kaynak metodu aciklayin. '
                     'Hasta kimlik bilgilerini atlas kaydina koymayin. Atlas v1 Enlil API sorgusu calistirmaz.'),
        dict(id='seed:rna',title='RNA QC kaynak akisinin kod haritasi',category='Veri kaynakları',
             source_kind='Kod',source_label='rna_qc_bundle.py',source_uri='rna_qc_bundle.py',linked_module='rna_qc_bundle.py',
             acquisition='v0.4.95 kaynak ZIP inde rna_qc_bundle.py modulunun bulundugu dogrulandi.',
             status='Gözden geçirilmeli',tags='RNA, fuzyon, teknik kalite',
             summary='RNA paneli icin DNA kapsama/FREEMIX alanlarini rapora otomatik tasimamak gerekir.',
             details='RNA QC notlarini RNA analizine bagli kaynak ve yontemle ayri kaydedin. '
                     'Bu ilk surumde RNA rapor cumlesi veya RNA kalite algoritmasi degistirilmez.'),
        dict(id='seed:ai',title='YZ incelemesi: kaynakli ve ayristirilmis not',category='Teknik hafıza',
             source_kind='Kod',source_label='ai_review.py / review_workspace_store.py',
             source_uri='ai_review.py',linked_module='ai_review.py',
             acquisition='v0.4.95 kaynak kodundaki AI review ve review workspace modulleri belirlendi.',
             status='Gözden geçirilmeli',tags='YZ, IGV, yorum, guven',
             summary='YZ onermesi ve klinik dogrulama birbirine karistirilmamali.',
             details='Atlas YZ notlarini "Yapay zekâ" kaynak turu ile ayri saklar. Kaynak linkleri ve belirsizlikler yazilir. '
                     'YZ kayitlari kendiliginden klinik rapora aktarilmaz.'),
        dict(id='seed:security',title='Bilgi Atlasi veri guvenligi ve sinirlar',category='Teknik hafıza',
             source_kind='Manuel',source_label='Atlas v1 yerel guvenlik tasarimi',
             acquisition='Atlas bagimsiz SQLite veritabani ve kaynak kodu analiz sinirlari uzerinden olusturuldu.',
             status='Yol gösterici',tags='KVKK, hasta verisi, guvenlik, yedekleme',
             summary='Bilgi Atlasi yalnizca teknik/bilimsel kaynak notlari icindir; hasta verileri otomatik kopyalanmaz.',
             details='Atlas veri dosyasi yereldir ancak kendi basina sifreli degildir. Klinik/hasta kimlik bilgisi, '
                     'sifre, token veya ham genomik analiz dosyasi atlas notlarina eklenmemelidir. '
                     'Yedek ve disari aktarma dosyalarinin da erisim izinlerini koruyun. '
                     'Programin mevcut veri tabanlari, genomik raporlar ve QC hesaplari bu modul tarafindan degistirilmez.'),
    ]
    count = 0
    for record in records:
        rid = record.pop('id')
        if store.seed_entry(rid,**record):
            count += 1
    return count
