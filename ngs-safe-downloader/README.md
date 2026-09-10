# NGS Safe Downloader

Windows üzerinde büyük NGS ham verilerini daha güvenli ve kesintiye dayanıklı biçimde indirmek için hazırlanmış araçtır. Özellikle Nextcloud public WebDAV paylaşımlarında dosyaları tek tek indirir, yarım kalan aktarımları yönetir ve indirme sonrasında bütünlük kontrolleri uygular.

> Bu araç fiziksel HDD/SSD tesliminin yerine geçtiği iddiasında değildir. Amaç, ağ üzerinden yapılan büyük veri aktarımlarında kopma, eksik dosya ve sessiz veri bozulması riskini azaltmaktır.

## Temel özellikler

- Kesilen indirmeleri `.part` dosyalarından sürdürebilme
- Adaptif paralel bağlantı ve sunucu koruma modu
- Eksik veya hatalı dosyaları otomatik yeniden indirme
- Dosya boyutu kontrolü
- Vendor MD5/SHA checksum doğrulaması
- Yerel SHA-256 kayıtları
- Opsiyonel derin FASTQ.GZ / GZIP CRC bütünlük testi
- İndirme ve doğrulama sonuçlarını `_NGS_RAPOR` klasöründe saklama
- Oturum başlangıç/bitiş zamanı, süre ve final durum özeti

## Kurulum / kurtarma

1. Bu klasördeki `BASLAT_NGS_INDIRICI.bat` dosyasını indirin.
2. Boş bir klasöre koyup çift tıklayın.
3. Başlatıcı `latest.json` dosyasını kontrol eder, en güncel paketi indirir ve SHA-256 ile doğrular.
4. `NGS_Safe_Downloader.py` yerelde yoksa otomatik kurulur; eskiyse güncellenir.
5. Bir önceki uygulama `NGS_Safe_Downloader.previous.py` olarak saklanır.

GitHub erişilemiyorsa güncelleme kontrolü atlanır; yerelde mevcut uygulama varsa çalışmaya devam eder. NGS veri aktarımı güncelleme servisine bağımlı değildir.

## Güncel sürüm

v2.0

## Kullanım notu

Normal kullanımda programı doğrudan Python dosyasından değil `BASLAT_NGS_INDIRICI.bat` üzerinden açın; otomatik sürüm kontrolü bu başlatıcıda yapılır.

Bu depoda herhangi bir kurum/şirket adına ait örnek veri indirme bağlantısı veya gerçek paylaşım adresi tutulmaz.
