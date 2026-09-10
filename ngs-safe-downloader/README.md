# NGS Safe Downloader

Windows üzerinde büyük NGS ham verilerini kesintiye dayanıklı ve doğrulanabilir biçimde indirmek için hazırlanmış yardımcı araçtır. Fiziksel HDD/SSD aktarımının alternatifi değildir; ağ üzerinden aktarım gerektiğinde indirme güvenliğini ve izlenebilirliğini artırmayı amaçlar.

## Ana akış

`İndir / devam et → otomatik doğrula → sorunlu dosyayı yeniden indir → yeniden doğrula → sıfır sorunla tamamla`

- Ağ veya elektrik kesintisinden sonra yarım dosyalardan devam eder.
- Bağlantıya göre paralelliği uyarlayabilir ve sunucu sorunlarında bekleme/koruma moduna geçebilir.
- Eksik, boyutu hatalı, checksum uyuşmazlığı olan veya FASTQ.GZ bütünlük kontrolünden geçmeyen dosyaları otomatik yeniden indirir.
- Final sorun sayısı sıfır olmadan işi `TAMAMLANDI` olarak işaretlemez.

## v2.4 ana ekran

Ana ekran teknik log okumadan izlenebilen sade bir sistem bakım / disk onarım dashboard'u olarak düzenlendi.

- Paylaşım bağlantısının yanında canlı bağlantı durumu: bağlanıyor / bağlı / bekleniyor / hata
- Anlık internet hızı ve aktif bağlantı sayısı
- Toplam veri ve dosya sayısı
- Tamamlanan dosya / veri miktarı
- Kalan dosya / veri miktarı ve yarım dosya sayısı
- Hatalı dosya sayısı
- Otomatik yeniden indirilecek dosya kuyruğu ve yeniden indirilen dosya sayısı
- Büyük genel ilerleme çubuğu
- Tahmini bitiş saati ve kalan süre
- Geçen süre
- `İndirme / Devam`, `Doğrulama`, `Hatalıları Düzelt` aşamalarının ayrı canlı durumu

Ayrıntılar gerektiğinde sekmelerden görülebilir: `Dosya görünümü`, `Hatalar / yeniden indirme`, `Canlı doğrulama`, `Disk karşılaştırma`, `Canlı log`. Yollar ve bakım seçenekleri ana ekranı kalabalıklaştırmaması için açılır ayrıntı alanında tutulur.

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
- Program/sistem tarafında yalnız uygulama, başlatıcı ve çok küçük ayar/son iş bilgileri

İstenirse, iş tamamen ve hatasız bittikten sonra çalışma kayıtları temizlenebilir.

## İki klasörü karşılaştır

Ağ üzerinden indirilen veri ile fiziksel HDD/SSD üzerinde gelen kopya karşılaştırılabilir. Önce göreli dosya yolu ve boyut kontrol edilir; aynı boyuttaki dosya çiftleri SHA-256 ile baştan sona okunur.

Arayüzde HDD inceleme araçlarına benzer küçük durum kareleri kullanılır: yeşil birebir aynı, kırmızı farklı, sarı yalnız bir tarafta, gri henüz kontrol edilmedi.

Karşılaştırma raporu da harici diskteki `_NGS_WORK/Comparisons` alanında tutulur.

## Kurulum / kurtarma

1. Yalnız `BASLAT_NGS_INDIRICI.bat` dosyasını boş bir klasöre indirin.
2. Çift tıklayın.
3. Başlatıcı `latest.json` dosyasını kontrol eder, güncel paketi indirir ve SHA-256 ile doğrular.
4. Uygulama yerelde yoksa otomatik kurulur; eskiyse güncellenir.
5. GitHub erişilemiyorsa, yerelde uygulama varsa mevcut sürümle çalışmaya devam eder.

## Güncel sürüm

**v2.4**

Bu depoda herhangi bir kurum/şirkete ait gerçek veya örnek NGS paylaşım bağlantısı tutulmaz.
