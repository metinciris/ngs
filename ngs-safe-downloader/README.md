# NGS Safe Downloader

Windows üzerinde büyük NGS ham verilerini kesintiye dayanıklı ve doğrulanabilir biçimde indirmek için hazırlanmış yardımcı araçtır. Fiziksel HDD/SSD aktarımının alternatifi değildir; ağ üzerinden aktarım gerektiğinde kopma, eksik dosya ve sessiz veri bozulması riskini azaltmayı amaçlar.

## Ana akış

`İndir / devam et → doğrula → sorunlu dosyayı yeniden indir → yeniden doğrula → tam doğrulanmış olarak bitir`

- Ağ veya elektrik kesintisinden sonra yarım dosyalardan devam eder.
- Bağlantıya göre paralelliği uyarlayabilir ve sunucu sorunlarında bekleme/koruma moduna geçebilir.
- Eksik, boyutu hatalı, checksum uyuşmazlığı olan veya FASTQ.GZ bütünlük kontrolünden geçmeyen dosyaları otomatik yeniden indirir.
- İndirme tamamlanması ile final doğrulama birbirinden ayrı tutulur.

## v2.5 doğrulama

v2.5, GNU `md5sum` biçimindeki checksum yan dosyalarını daha güvenli eşleştirir. Yan dosyada uzak sistemden kalmış mutlak Linux yolu bulunsa bile, `sample.fq.gz.md5sum` dosyası güvenli biçimde ilgili `sample.fq.gz` ile eşleştirilebilir.

Doğrulama ekranı ve raporlar artık şu aşamaları ayrı gösterir:

- checksum yan dosyası bulundu
- yan dosya okundu ve doğru FASTQ ile eşleştirildi
- yerel MD5/SHA kaynak değeriyle eşleşti
- FASTQ.GZ GZIP/CRC testi başarılı

Örnek final durum:

`sidecar 16/16 • MD5/SHA 16/16 • CRC 16/16 • TAM DOĞRULANDI`

Yeşil başarı yalnız final doğrulama gerçekten tamamlandığında gösterilir. `NGS_DOWNLOAD_STATUS.json`, indirme sonunda önce `download_complete_pending_verification`; doğrulama sonunda ise `verified`, `verification_warning` veya `verification_failed` olarak güncellenir.

## Ana ekran

Ana ekran teknik log okumadan izlenebilen sade bir sistem bakım / disk onarım dashboard'udur.

- Paylaşım bağlantısının yanında canlı bağlantı durumu
- Anlık internet hızı ve aktif bağlantı sayısı
- Toplam veri ve dosya sayısı
- Tamamlanan ve kalan dosya/veri miktarı
- Hatalı dosya sayısı
- Otomatik yeniden indirme kuyruğu
- Büyük genel ilerleme çubuğu
- Tahmini bitiş saati, kalan süre ve geçen süre
- İndirme / doğrulama / onarma aşamalarının ayrı durumu

Ayrıntılar gerektiğinde `Dosya görünümü`, `Hatalar / yeniden indirme`, `Canlı doğrulama`, `Disk karşılaştırma` ve `Canlı log` sekmelerinden izlenebilir.

## Disk düzeni

Programın bulunduğu diskin şişmemesi özellikle gözetilir.

- Hedef veri klasörü: yalnız gerçek indirilen dosyalar
- Hedef harici diskte `_NGS_PARCA`: yarım indirmeler
- Hedef harici diskte `_NGS_WORK`: log, manifest, doğrulama ve karşılaştırma raporları
- Program/sistem tarafında yalnız uygulama, başlatıcı ve çok küçük ayar/son iş bilgileri

## İki klasörü karşılaştır

Ağ üzerinden indirilen veri ile fiziksel HDD/SSD üzerinde gelen kopya göreli yol + boyut + SHA-256 ile salt-okunur biçimde karşılaştırılabilir. Sonuçlar HDD inceleme araçlarına benzer küçük durum kareleriyle gösterilir.

## Kurulum / güncelleme

1. Yalnız `BASLAT_NGS_INDIRICI.bat` dosyasını boş bir klasöre indirin.
2. Çift tıklayın.
3. Başlatıcı `latest.json` dosyasını kontrol eder.
4. Güncel paketi indirir; paket SHA-256 ve uygulama SHA-256 değerlerini doğrular.
5. Uygulama yerelde yoksa kurar, eskiyse günceller.

## Güncel sürüm

**v2.5**

Bu depoda herhangi bir kurum/şirkete ait gerçek veya örnek NGS paylaşım bağlantısı tutulmaz.
