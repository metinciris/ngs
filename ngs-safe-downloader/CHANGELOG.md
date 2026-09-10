# NGS Safe Downloader

## v2.4 - 2026-09-10
- Ana arayüz sistem bakım/disk onarım araçlarına benzer sade bir dashboard olarak yenilendi.
- Paylaşım bağlantısının yanına canlı bağlantı durumu eklendi.
- Ana ekrana internet hızı, toplam, tamam, kalan, hatalı ve işlenecek hatalı sayaçları eklendi.
- Tahmini bitiş zamanı ve geçen süre ana ekranda görünür hale getirildi.
- Otomatik yeniden indirme alanı; kuyruk, tur ve yeniden indirilen dosya sayısını canlı gösterir.
- Yeni `Hatalar / yeniden indirme` sekmesi ile bulunan sorunların ve çözülme durumunun ayrıntısı izlenebilir.
- `Dosya görünümü`, `Canlı doğrulama`, `Disk karşılaştırma` ve `Canlı log` ayrıntı sekmelerinde korunmuştur.
- Yollar ve bakım seçenekleri ana ekranı kalabalıklaştırmaması için açılır ayrıntı alanına taşındı.
- İndirme/doğrulama/onarma motoru v2.3 ile aynı güvenli çalışma mantığını korur.
- GitHub otomatik güncelleme kanalı v2.4'e taşındı.
- Büyük çalışma verileri yine hedef HDD üzerindeki `_NGS_PARCA` ve `_NGS_WORK` alanlarında tutulur; program diski şişmez.

## v2.3 - 2026-09-10
- `İki Klasörü Karşılaştır` modu eklendi: göreli yol + boyut + SHA-256 ile ağ kopyası ve fiziksel HDD/SSD kopyası karşılaştırılabilir.
- Karşılaştırma sonuçları HDD inceleme/onarım araçlarına benzer küçük durum kareleriyle gösterilir.
- Ana otomatik akış: indir/devam → doğrula → sorunluyu yeniden indir → yeniden doğrula → sıfır sorunla tamamla.
- FASTQ.GZ / GZIP CRC bütünlük kontrolü tam otomatik akışa dahil edildi.
- Programın bulunduğu diske büyük log, parça veya doğrulama verisi yazılmaz.
- Yarım indirmeler hedef harici diskte `_NGS_PARCA`, log/manifest/doğrulama/karşılaştırma kayıtları `_NGS_WORK` altında tutulur.
- Elektrik/ağ kesintisi sonrası önceki iş ve `.part` dosyaları algılanarak devam edilebilir.

## v2.0 - 2026-09-09
- Kalıcı GitHub dağıtım kanalı eklendi.
- Başlatıcı her açılışta `latest.json` kontrol eder.
- Yeni paket varsa SHA-256 doğrulaması sonrası günceller.
- Güncelleme başarısızsa mevcut sürüm korunur ve uygulama çalışmaya devam eder.
