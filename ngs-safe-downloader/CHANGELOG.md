# NGS Safe Downloader

## v2.6 - 2026-09-23
- Final doğrulama raporları artık uygulamanın bulunduğu klasördeki `NGS_Dogrulamalar` arşivine taşınır.
- Aktif iş sırasında kesinti/yeniden başlatma güvenliği için `_NGS_WORK` ve `_NGS_PARCA` hedef veri diski üzerinde tutulmaya devam eder.
- Final doğrulama tamamlandıktan sonra ilgili HDD `_NGS_WORK` iş klasörü otomatik temizlenir.
- Veri tam doğrulanmışsa artık gerekli olmayan ilgili `_NGS_PARCA` iş klasörü de otomatik temizlenir; boş üst geçici klasörler kaldırılır.
- Hedef veri klasörünün ve HDD'nin mümkün olduğunca yalnız gerçek NGS verileriyle kalması amaçlanır.
- Arayüzde doğrulama raporlarına erişim `Doğrulama arşivini aç` üzerinden program klasöründeki en son arşive yönlendirilir.
- v2.5 checksum/MD5/SHA/GZIP-CRC doğrulama mantığı korunur.

## v2.5 - 2026-09-23
- GNU `.md5sum` biçimi ve uzak sistemden kalmış mutlak Linux yolları güvenli biçimde destekleniyor.
- `sample.fq.gz.md5sum` yan dosyası, içerikte yabancı bir mutlak yol bulunsa bile ilgili yerel `sample.fq.gz` ile güvenli biçimde eşleştiriliyor.
- Doğrulama raporu artık `checksum yan dosyası bulundu`, `yan dosya okundu/eşleştirildi`, `kaynak MD5/SHA eşleşti` ve `GZIP/CRC` sayaçlarını ayrı gösteriyor.
- Ana final sonucu ör. `sidecar 16/16 • MD5/SHA 16/16 • CRC 16/16 • TAM DOĞRULANDI` biçiminde gösteriliyor.
- İndirme aşamasının bitmesi artık `NGS_DOWNLOAD_STATUS.json` içinde nihai başarı sayılmıyor; durum `download_complete_pending_verification` olarak tutuluyor.
- Final doğrulama sonrasında durum JSON'u `verified`, `verification_warning` veya `verification_failed` olarak güncelleniyor ve checksum/CRC sayaçları kaydediliyor.
- Otomatik ve `Sadece doğrula` modunda yeşil başarı yalnız `full_verified` olduğunda gösteriliyor.
- GNU-style checksum yan dosyalarıyla yapılan parser testleri doğru FASTQ eşleştirmesini doğruladı.

## v2.4 - 2026-09-10
- Ana arayüz sistem bakım/disk onarım araçlarına benzer sade bir dashboard olarak yenilendi.
- Paylaşım bağlantısının yanına canlı bağlantı durumu eklendi.
- Ana ekrana internet hızı, toplam, tamam, kalan, hatalı ve işlenecek hatalı sayaçları eklendi.
- Tahmini bitiş zamanı ve geçen süre ana ekranda görünür hale getirildi.
- Otomatik redownload alanı; kuyruk, tur ve yeniden indirilen dosya sayısını canlı gösterir.
- `Hatalar / yeniden indirme`, dosya görünümü, canlı doğrulama, disk karşılaştırma ve canlı log sekmeleri eklendi/korundu.
- Büyük çalışma verileri hedef HDD üzerindeki `_NGS_PARCA` ve `_NGS_WORK` alanlarında tutulur.

## v2.3 - 2026-09-10
- İki klasörü göreli yol + boyut + SHA-256 ile karşılaştırma eklendi.
- Tam otomatik doğrulama/onarma akışı ve harici çalışma alanı düzeni güçlendirildi.

## v2.0 - 2026-09-09
- Kalıcı GitHub dağıtım kanalı eklendi.
- Başlatıcı her açılışta `latest.json` kontrol eder.
- Yeni paket varsa SHA-256 doğrulaması sonrası günceller.
- Güncelleme başarısızsa mevcut sürüm korunur ve uygulama çalışmaya devam eder.
