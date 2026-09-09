# NGS Safe Downloader

Windows üzerinde Gen Cloud Drive / Nextcloud public WebDAV üzerinden büyük NGS ham verilerini güvenli indirmek için hazırlanmış araç.

## Kurulum / kurtarma

1. Bu klasördeki `BASLAT_NGS_INDIRICI.bat` dosyasını indirin.
2. Boş bir klasöre koyup çift tıklayın.
3. Başlatıcı `latest.json` dosyasını kontrol eder, en güncel paketi indirir ve SHA-256 ile doğrular.
4. `NGS_Safe_Downloader.py` yerelde yoksa otomatik kurulur; eskiyse güncellenir.
5. Bir önceki uygulama `NGS_Safe_Downloader.previous.py` olarak saklanır.

GitHub erişilemiyorsa güncelleme kontrolü başarısız olur fakat yerelde mevcut uygulama varsa onunla çalışmaya devam eder. NGS indirme işi güncelleme servisine bağımlı değildir.

## Güncel sürüm

v2.0

v2.0 ile kalıcı GitHub güncelleme kanalı eklendi. v1.9'daki adaptif indirme, sunucu koruma modu, `.part` devam desteği, `_NGS_PARCA`, otomatik hata onarımı, vendor MD5/SHA doğrulama ve opsiyonel derin FASTQ.GZ CRC kontrolü korunmuştur.

## Kullanım notu

Normal kullanımda programı doğrudan Python dosyasından değil `BASLAT_NGS_INDIRICI.bat` üzerinden açın; otomatik sürüm kontrolü bu başlatıcıda yapılır.
