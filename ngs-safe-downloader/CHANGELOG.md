# NGS Safe Downloader

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
