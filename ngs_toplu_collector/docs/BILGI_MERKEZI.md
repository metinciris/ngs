# NGS Bilgi ve Sistem Hafızası — ilk sürüm

Tarih: 23 Kasım 2026 | Durum: **bağımsız önizleme, uygulamaya kurulmadı**

## Amaç

Kodun binlerce satır olması ve geçmiş YZ konuşmalarının güvenilir bir sistem kaydı olmaması nedeniyle, her teknik bilginin **kaynağını, nasıl toplandığını, nasıl doğrulandığını, kod bağlantısını, eksik veri davranışını ve güncelleme durumunu** tek merkezde açıklamak.

Bu aşama bir **bilgi merkezi prototipi**dir; gerçek hasta dosyasına veya yerel veritabanına dokunan bir uygulama yaması değildir.

## Açma

1. Bu daldaki `knowledge_center/index.html` ve `knowledge_center/catalog.json` dosyalarını **aynı klasöre** indirin.
2. `index.html` dosyasını tarayıcıda açın. Tarayıcı yerel JSON okumasını engellerse üstteki **Yerel katalog seç (.json)** düğmesine basın ve `catalog.json` seçin.
3. Alternatif olarak projenin `knowledge_center` klasöründe `python -m http.server 8765` çalıştırın, sonra `http://localhost:8765/` adresini açın.

Sunucu varsa yalnızca yerel HTTP sunucusudur; dosya okumak dışında hiçbir uygulama komutu çalıştırmaz. Python sunucusunu ağdan erişilebilir bir adrese açmayın; hassas dosyalar içeren üst dizinden çalıştırmayın.

## Neler var?

- Türkçe serbest metin araması ve kategori/durum filtreleri.
- Uzun, aşağı doğru açılan kayıtlar; tümünü aç/kapat.
- Her kayıt için elde etme yolu, doğrulama, eksik veri politikası, kod/doküman referansları ve bağlantılar.
- Yazdır / PDF (tarayıcı üzerinden).
- JSON katalogla yeni kayıt ekleme.
- **DNA derinlik 161 KB paket: KURULMADI** — test başarılı olması klinikte aktif olduğu anlamına gelmez.
- Genel teknik notlar ile kullanıcı geri bildirimi ve dış kaynakların farklı durum alanları.

## Katalog kaydı

`catalog.json` içindeki her `items[]` nesnesi:

```json
{
  "id": "essiz-anahtar",
  "category": "QC",
  "title": "Başlık",
  "status": "BELGELENDI",
  "summary": "Kısa, özgün sentez",
  "how": "Veri nasıl elde edilir?",
  "validation": "Nasıl kontrol edilir?",
  "missing": "Eksik ya da belirsizse ne yapılır?",
  "code_refs": ["modul.py", "docs/aciklama.md"],
  "sources": [{"label": "Resmî referans", "url": "https://example.org", "kind": "external"}],
  "checked_on": "2026-11-23"
}
```

Bu sürüm **salt okuma** amaçlıdır. Ekrandaki değişiklikler kaydedilmez; kalıcı katalog değişiklikleri yalnızca Git üzerinden gözden geçirilerek yapılır. Otomatik web taraması, kod indeksleme, rapor veri kaynağı eşlemesi ve yerel uygulama menüsüne gömme **henüz uygulanmadı**.

## Durumları nasıl okuyalım?

- `BELGELENDI`: Dokümante edilmiş mimari/iş akışı; canlı çalışma doğrulaması değildir.
- `KULLANICI_BILDIRIMI`: Laboratuvarın bildirdiği ihtiyaç/durum; kodla doğrulanmalıdır.
- `DIS_KAYNAK`: Genel dış teknik açıklama; yerel verinin yerine geçmez.
- `KURULMADI`: Paket hazırlanmış fakat çalışan uygulamaya kurulmamıştır.

## Güvenlik ve entegrasyon

- Mevcut `C:\ngs\ngs_toplu_collector` dizinine **otomatik kurulum veya dosya yazma yoktur**.
- GitHub depodaki tam kaynak v0.4.83, kullanıcının güncel yerel uygulamasıyla **eşitlenmemiştir**. Güncel yerel kaynak/diff görülmeden `app.py`, `db.py`, `qc_enlil.py` veya raporlamaya otomatik ekleme yapılmaz.
- `DATA/`, `OUTPUT/`, `AI_EXPORT/`, `PROFILE/`, `DIAGNOSTICS/`, SQLite, gerçek RUN/sample UUID, hasta bilgileri, gizli kurum URL'leri ve tokenlar bu bilgi merkezine veya GitHub'a konulmaz.
- Resmî yayın/teknik belgelerden kısa ve özgün sentez tutulur. Kaynak bilgisi otomatik klinik karar haline dönüştürülmez.
- Gelecek aşamada **kaynak kayıtları ayrı**, **normalize model ayrı**, **UI ayrı** tutulmalı. Uygulama modülü mümkünse yerel salt-okuma adapter üzerinden bağlanmalıdır.

## Bir sonraki geliştirme (bu PR kapsamı dışında)

1. Güncel yerel uygulama dosyalarının kişisel veri içermeyen güvenli kaynak snapshot'ı ile doğrulanması.
2. Rapor alanı → gerçek QC TSV/JSON sütunu → kod fonksiyonu → doğrulama testi ilişkilerini gösteren kaynak izleme tablosu.
3. Manuel ve Enlil kaynaklı alanlar için özel, yalnız yerelde tutulan veri kökeni kaydı.
4. Arayüz menüsünden `Bilgi Merkezi` bağlantısı; otomatik canlı veri yazımı olmadan.
5. Güvenli DNA derinlik paketinin ayrıca kurulumu ve canlı QC kaynak doğrulaması. Bu PR o paketi yüklemez.
