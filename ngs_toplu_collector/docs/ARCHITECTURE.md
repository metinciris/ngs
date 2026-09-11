# NGS Toplu Collector v0.1.6 - Mimari

## Tarayıcı
Playwright persistent Chromium tek bir Browser Worker thread içinde yaşar. `/runs?page=1&perPage=7` ana sayfası ana sekmedir ve kapanmaz. Run / Clinical / SNV / CNV / Fusion / QC child sayfaları sırayla açılır ve işlem sonunda kapanır.

## Görev kuyruğu
UI aynı anda yalnız bir tarayıcı işi başlatabilir. Yeni DNA/RNA/Tüm Eksikler işi çalışan işin arkasına yığılamaz.

- Mevcut Adım Sonrası Durdur: stop flag + bekleyen komutların temizlenmesi; mevcut teknik adım biter, yeni adım/hasta başlamaz.
- HEMEN DURDUR: bekleme/download döngüleri de kesilir.

## Teknik kimlik
Ana kayıt anahtarı:
`runUuid :: sampleUuid :: sampleType`

Gösterim kimliği NGS analiz platformu'teki teknik sample etiketidir ve olduğu gibi korunur.

- MP001-26 -> case_family MP001-26
- MP001--26 -> case_family MP001-26, is_rerun=1
- MP002-26--MP003-26 -> bileşik etiket olarak korunur, otomatik rerun sayılmaz

## Clinical
Clinical sayfası artık extension kullanmaz. Python doğrudan accordion/pagination DOM'unu gezip bütün gen bloklarını ve varyant satırlarını JSON'a yazar. Toplam gen sayısı doğrulaması başarısızsa görev `done` olmaz.

## Ham kalite verisi
Run depth ve indirilen SNV/CNV/QC dosyaları teknik kayıt bazında ayrıdır. Downstream Mutasyon Havuzu, varyant gözlemlerini QC ile aynı record_key/case_family üzerinden ilişkilendirecektir.

## v0.2.2 UI durum makinesi

Tarayıcı işlemleri UI tarafından üç aşamalı durum makinesi ile sunulur: `open_browser -> scan_runs -> complete_latest`. Worker busy iken yeni browser işi başlatılamaz; yalnız stop istekleri aktiftir. `job_done` payload'ı tamamlanma / hata / remaining bilgisini UI'a aktarır ve UI yalnız gerçek `all_complete` durumunda güvenli-kapat mesajı verir.
