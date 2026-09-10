# NGS Safe Downloader

Windows üzerinde büyük NGS ham verilerini kesintiye dayanıklı ve doğrulanabilir biçimde indirmek için hazırlanmış yardımcı araçtır. Fiziksel HDD/SSD aktarımının alternatifi değildir; ağ üzerinden aktarım gerektiğinde indirme güvenliğini ve izlenebilirliğini artırmayı amaçlar.

## Ana akış

`İndir / devam et → otomatik doğrula → sorunlu dosyayı yeniden indir → yeniden doğrula → sıfır sorunla tamamla`

- Ağ veya elektrik kesintisinden sonra yarım dosyalardan devam eder.
- Bağlantıya göre paralelliği uyarlayabilir ve sunucu sorunlarında bekleme/koruma moduna geçebilir.
- Eksik, boyutu hatalı, checksum uyuşmazlığı olan veya FASTQ.GZ bütünlük kontrolünden geçmeyen dosyaları otomatik yeniden indirir.
- Final sorun sayısı sıfır olmadan işi `TAMAMLANDI` olarak işaretlemez.

## Doğrulama

Tam otomatik modda kullanıcı ayrıca doğrulama seçmek zorunda değildir:

- Uzak dosya boyutu
- Kaynak MD5/SHA checksum (varsa)
- Yerel SHA-256
- FASTQ.GZ / GZIP CRC ve stream bütünlük kontrolü

## Disk düzeni

Programın bulunduğu diskin şişmemesi özellikle gözetilir.

- Hedef veri klasörü: yalnız gerçek indirilen dosyalar
- Hedef harici diskte `_NGS_PARCA`: yarım indirmeler
- Hedef harici diskte `_NGS_WORK`: log, manifest, doğrulama ve karşılaştırma raporları
- Program/sistem tarafında yalnız küçük ayar ve son iş bilgileri

İstenirse, iş tamamen ve hatasız bittikten sonra çalışma kayıtları temizlenebilir.

## İki klasörü karşılaştır

Ağ üzerinden indirilen veri ile fiziksel HDD/SSD üzerinde gelen kopya karşılaştırılabilir. Önce göreli dosya yolu ve boyut kontrol edilir; aynı boyuttaki dosya çiftleri SHA-256 ile baştan sona okunur.

Arayüzde HDD inceleme araçlarına benzer küçük durum kareleri kullanılır:

- Yeşil: SHA-256 birebir aynı
- Kırmızı: boyut veya içerik farklı
- Sarı: yalnız bir tarafta
- Gri: henüz kontrol edilmedi

Karşılaştırma raporu da harici diskteki `_NGS_WORK/Comparisons` alanında tutulur.

## Kurulum / kurtarma

1. Yalnız `BASLAT_NGS_INDIRICI.bat` dosyasını boş bir klasöre indirin.
2. Çift tıklayın.
3. Başlatıcı `latest.json` dosyasını kontrol eder, güncel paketi indirir ve SHA-256 ile doğrular.
4. Uygulama yerelde yoksa otomatik kurulur; eskiyse güncellenir.
5. GitHub erişilemiyorsa, yerelde uygulama varsa mevcut sürümle çalışmaya devam eder.

## Güncel sürüm

**v2.3**

Bu depoda herhangi bir kurum/şirkete ait gerçek veya örnek NGS paylaşım bağlantısı tutulmaz.
