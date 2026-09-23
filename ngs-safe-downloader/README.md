# NGS Safe Downloader

Windows üzerinde büyük NGS ham verilerini kesintiye dayanıklı ve doğrulanabilir biçimde indirmek için hazırlanmış yardımcı araçtır. Fiziksel HDD/SSD aktarımının alternatifi değildir; ağ üzerinden aktarım gerektiğinde kopma, eksik dosya ve sessiz veri bozulması riskini azaltmayı amaçlar.

## Ana akış

`İndir / devam et → doğrula → sorunlu dosyayı yeniden indir → yeniden doğrula → tam doğrulanmış olarak bitir`

- Ağ veya elektrik kesintisinden sonra yarım dosyalardan devam eder.
- Bağlantıya göre paralelliği uyarlayabilir ve sunucu sorunlarında bekleme/koruma moduna geçebilir.
- Eksik, boyutu hatalı, checksum uyuşmazlığı olan veya FASTQ.GZ bütünlük kontrolünden geçmeyen dosyaları otomatik yeniden indirir.
- İndirme tamamlanması ile final doğrulama birbirinden ayrı tutulur.

## Doğrulama

GNU `.md5sum` biçimindeki checksum yan dosyaları desteklenir. Yan dosyada uzak sistemden kalmış mutlak Linux yolu bulunsa bile, `sample.fq.gz.md5sum` güvenli biçimde ilgili yerel `sample.fq.gz` ile eşleştirilebilir.

Doğrulama ekranı ve raporlar şu aşamaları ayrı gösterir:

- checksum yan dosyası bulundu
- yan dosya okundu ve doğru FASTQ ile eşleştirildi
- yerel MD5/SHA kaynak değeriyle eşleşti
- FASTQ.GZ GZIP/CRC testi başarılı

Örnek final durum:

`sidecar 16/16 • MD5/SHA 16/16 • CRC 16/16 • TAM DOĞRULANDI`

Yeşil başarı yalnız final doğrulama gerçekten tamamlandığında gösterilir. `NGS_DOWNLOAD_STATUS.json`, indirme sonunda önce `download_complete_pending_verification`; doğrulama sonunda ise `verified`, `verification_warning` veya `verification_failed` olarak güncellenir.

## v2.6: temiz HDD ve doğrulama arşivi

Aktif iş sırasında kesinti güvenliği için yarım indirmeler ve çalışma kayıtları hedef veri diski üzerinde tutulur. Final doğrulama tamamlandığında doğrulama raporları uygulamanın bulunduğu klasördeki `NGS_Dogrulamalar` arşivine taşınır.

Örnek:

```text
NGS Safe Downloader/
  NGS_Safe_Downloader.py
  NGS_Dogrulamalar/
    QC FASTQ_<is_kimligi>_<tarih_saat>/
      DOGRULAMA_SONUCU.txt
      DOGRULAMA_DETAY.csv
      NGS_DOWNLOAD.log
      NGS_DOWNLOAD_MANIFEST.csv
      NGS_DOWNLOAD_STATUS.json
      SON_DURUM.txt
```

Final doğrulama sonrasında ilgili HDD çalışma klasörü temizlenir. Veri tam doğrulanmışsa artık gerekli olmayan ilgili `_NGS_PARCA` alanı da kaldırılır; üst klasörler boşsa silinir. Böylece hedef HDD'de mümkün olduğunca yalnız gerçek NGS verileri kalır.

Arayüzdeki **Doğrulama arşivini aç** düğmesi seçili işin en son doğrulama arşivini açar.

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

İşlem sürerken:

- Hedef veri klasörü: gerçek indirilen dosyalar
- Hedef veri diski `_NGS_PARCA`: yarım indirmeler ve devam bilgisi
- Hedef veri diski `_NGS_WORK`: aktif iş log/manifest/doğrulama kayıtları

Final doğrulama sonrasında rapor arşivi program klasöründeki `NGS_Dogrulamalar` altına alınır ve ilgili geçici HDD çalışma alanları yukarıdaki güvenlik kurallarına göre temizlenir.

## İki klasörü karşılaştır

Ağ üzerinden indirilen veri ile fiziksel HDD/SSD üzerinde gelen kopya göreli yol + boyut + SHA-256 ile salt-okunur biçimde karşılaştırılabilir. Sonuçlar HDD inceleme araçlarına benzer küçük durum kareleriyle gösterilir.

## Kurulum / güncelleme

1. Yalnız `BASLAT_NGS_INDIRICI.bat` dosyasını boş bir klasöre indirin.
2. Çift tıklayın.
3. Başlatıcı `latest.json` dosyasını kontrol eder.
4. Güncel paketi indirir; paket SHA-256 ve uygulama SHA-256 değerlerini doğrular.
5. Uygulama yerelde yoksa kurar, eskiyse günceller.

## Güncel sürüm

**v2.6**

Bu depoda herhangi bir kurum/şirkete ait gerçek veya örnek NGS paylaşım bağlantısı tutulmaz.
