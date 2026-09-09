# NGS Safe Downloader

## v2.0 - 2026-09-09
- Kalıcı GitHub dağıtım kanalı eklendi.
- Başlatıcı her açılışta `latest.json` kontrol eder.
- Yeni paket varsa SHA-256 doğrulaması sonrası günceller.
- Güncelleme başarısızsa mevcut sürüm korunur ve uygulama çalışmaya devam eder.
- v1.9'daki otomatik hata onarımı, adaptif bağlantı, MD5/SHA, FASTQ.GZ CRC ve parça yönetimi korunur.
